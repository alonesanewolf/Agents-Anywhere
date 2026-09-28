"""Per-host conversation ownership, canonical following and follower requests.

Resnapshot on a revision gap is an AA recovery extension. The inspected IDE
ignores gaps. Execution and persistence belong to the injected owner backend.
"""

import asyncio
import inspect
from copy import deepcopy
from dataclasses import dataclass, field

from loguru import logger

from connector.runtimes.codex.domain.activity import activity_patch, activity_signature

from .state import (
    apply_patches,
    enumerate_turns,
    history_complete,
    read_context_matches,
)
from .wire import METHOD_VERSIONS, IpcError

# Installed routers allow 10 seconds per discovery candidate, independently of
# request timeoutMs. Leave room for their explicit no-client-found response.
OWNER_DISCOVERY_TIMEOUT = 12

FOLLOWER_METHODS = tuple(
    name for name in METHOD_VERSIONS if name.startswith("thread-follower-")
)
EVENT_METHODS = {
    "thread-queued-followups-changed",
    "thread-read-state-changed",
    "thread-archived",
    "thread-unarchived",
}


@dataclass(frozen=True)
class OwnerInfo:
    client_id: str
    supports_untrusted_app_input: bool = False


@dataclass
class _Owned:
    state: dict
    revision: int = 1
    supports_untrusted_app_input: bool = False
    followers: set[str] = field(default_factory=set)


@dataclass
class _Followed:
    owner: OwnerInfo | None = None
    state: dict | None = None
    revision: int | None = None
    snapshots: set[asyncio.Future] = field(default_factory=set)


async def _invoke(callback, *args):
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


class CoordinationPeer:
    def __init__(
        self,
        client,
        *,
        host_id="local",
        on_state=None,
        owner_handler=None,
        on_event=None,
        read_state_context=None,
        max_followed=128,
    ):
        if max_followed < 1:
            raise ValueError("max_followed must be positive")
        self.client = client
        self.host_id = host_id
        self.on_state = on_state
        self.owner_handler = owner_handler
        self.on_event = on_event
        self.read_state_context = read_state_context
        self.max_followed = max_followed
        self._owned: dict[str, _Owned] = {}
        self._followed: dict[str, _Followed] = {}
        self._queues: dict[str, dict] = {}
        self._activity = {}
        self._activity_clock = 0
        self._waiters: dict[str, set] = {}
        self._follow_locks: dict[str, asyncio.Lock] = {}
        self._restores: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._removers = []
        self._notifications = asyncio.Queue()
        self._notification_task = None
        self._closed = False
        self._started = False
        self._self_id = None

    async def start(self):
        if self._closed:
            raise IpcError("disposed")
        if not self._started:
            self._started = True
            self._removers.append(self.client.add_broadcast_handler(self._on_broadcast))
            for method in ("thread-owner-discovery", *FOLLOWER_METHODS):
                self._removers.append(
                    self.client.add_request_handler(
                        method, self._can_handle, self._handle_request
                    )
                )
            self._notification_task = asyncio.create_task(self._notify_loop())
        await self.client.start()
        await self.client.wait_initialized()

    def is_owner(self, thread_id):
        return thread_id in self._owned

    def is_follower(self, thread_id):
        """True also while a subscribed thread awaits reconnect/resume."""
        return thread_id in self._followed

    def get_owner(self, thread_id):
        if thread_id in self._owned:
            return OwnerInfo(
                self.client.client_id,
                self._owned[thread_id].supports_untrusted_app_input,
            )
        followed = self._followed.get(thread_id)
        return followed.owner if followed else None

    def get_state(self, thread_id):
        record = self._owned.get(thread_id) or self._followed.get(thread_id)
        return deepcopy(record.state) if record else None

    def get_revision(self, thread_id):
        record = self._owned.get(thread_id) or self._followed.get(thread_id)
        return record.revision if record else None

    def get_queue(self, thread_id):
        return deepcopy(self._queues.get(thread_id))

    def _require_open(self):
        if self._closed:
            raise IpcError("disposed")

    async def discover_owner(
        self,
        thread_id,
        *,
        timeout=OWNER_DISCOVERY_TIMEOUT,
        expected_owner_client_id=None,
    ):
        self._require_open()
        if self.is_owner(thread_id) and expected_owner_client_id in (
            None,
            self.client.client_id,
        ):
            return self.get_owner(thread_id)
        try:
            response = await self.client.request(
                "thread-owner-discovery",
                {"hostId": self.host_id, "conversationId": thread_id},
                target_client_id=expected_owner_client_id,
                timeout=timeout,
            )
        except IpcError as exc:
            if exc.code == "no-client-found" and expected_owner_client_id is None:
                return None
            raise
        result = response["result"]
        if not isinstance(result, dict):
            raise IpcError("invalid-owner-response")
        return OwnerInfo(
            response["handledByClientId"],
            result.get("supportsUntrustedAppInput") is True,
        )

    async def follow(self, thread_id, *, timeout=OWNER_DISCOVERY_TIMEOUT):
        self._require_open()
        if self.is_owner(thread_id):
            return self.get_state(thread_id)
        if thread_id not in self._followed and len(self._followed) >= self.max_followed:
            raise IpcError("follow-limit")
        # Register intent before any await. Concurrent follows share one bounded slot.
        existed = thread_id in self._followed
        record = self._followed.setdefault(thread_id, _Followed())
        lock = self._follow_locks.setdefault(thread_id, asyncio.Lock())
        try:
            async with asyncio.timeout(timeout), lock:
                owner = await self.discover_owner(thread_id, timeout=timeout)
                if self._followed.get(thread_id) is not record:
                    raise IpcError("unfollowed")
                if owner is None:
                    self._invalidate(thread_id, "owner-unavailable")
                    if not existed:
                        self._followed.pop(thread_id, None)
                    return None
                if record.owner != owner:
                    self._invalidate(thread_id, "owner-changed")
                    record.owner = owner
                snapshot = asyncio.get_running_loop().create_future()
                record.snapshots.add(snapshot)
                try:
                    await self._send_follow(thread_id, True, [owner.client_id])
                    return await snapshot
                finally:
                    record.snapshots.discard(snapshot)
                    if not snapshot.done():
                        snapshot.cancel()
                    elif not snapshot.cancelled():
                        snapshot.exception()
        except TimeoutError as exc:
            raise IpcError("follow-timeout") from exc
        finally:
            # Don't remove locks with concurrent waiters; unfollow/close reclaims them.
            if thread_id not in self._followed and not lock.locked():
                self._follow_locks.pop(thread_id, None)

    async def _send_follow(self, thread_id, following, targets=None):
        await self.client.broadcast(
            "thread-stream-following-changed",
            {
                "conversationId": thread_id,
                "hostId": self.host_id,
                "following": following,
            },
            target_client_ids=targets,
        )

    async def unfollow(self, thread_id):
        record = self._followed.get(thread_id)
        if record is None:
            return
        self._invalidate(thread_id, "unfollowed")
        self._followed.pop(thread_id, None)
        self._queues.pop(thread_id, None)
        task = self._restores.pop(thread_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        self._follow_locks.pop(thread_id, None)
        # The local release is effective even if the connection cannot flush it.
        try:
            async with asyncio.timeout(1):
                await self._send_follow(thread_id, False)
        except (IpcError, TimeoutError):
            pass

    async def claim(self, thread_id, state, *, supports_untrusted_app_input=False):
        self._require_open()
        if not isinstance(state, dict):
            raise IpcError("invalid-state")
        if self.is_follower(thread_id):
            raise IpcError("already-following")
        old = self._owned.get(thread_id)
        self._owned[thread_id] = _Owned(
            deepcopy(state),
            (old.revision + 1) if old else 1,
            supports_untrusted_app_input,
            old.followers if old else set(),
        )
        revision = self._owned[thread_id].revision
        self._state_changed(thread_id)
        await self._snapshot(thread_id)
        await self.client.broadcast(
            "thread-stream-following-status-requested",
            {"conversationId": thread_id, "hostId": self.host_id},
        )
        return revision

    async def release(self, thread_id):
        self._activity.pop(thread_id, None)
        self._owned.pop(thread_id, None)
        self._queues.pop(thread_id, None)
        self._fail_waiters(thread_id, "ownership-released")

    async def publish_state(self, thread_id, state):
        self._require_open()
        record = self._owned.get(thread_id)
        if record is None:
            raise IpcError("not-owner")
        if not isinstance(state, dict):
            raise IpcError("invalid-state")
        record.state = deepcopy(state)
        record.revision += 1
        revision = record.revision
        self._state_changed(thread_id)
        await self._snapshot(thread_id)
        return revision

    async def publish_patches(self, thread_id, patches, *, accepted_text_changes=None):
        self._require_open()
        record = self._owned.get(thread_id)
        if record is None:
            raise IpcError("not-owner")
        result = apply_patches(record.state, patches)
        change = {
            "type": "patches",
            "baseRevision": record.revision,
            "revision": record.revision + 1,
            "patches": deepcopy(patches),
        }
        if accepted_text_changes is not None:
            change["acceptedTextChanges"] = deepcopy(accepted_text_changes)
        record.state = result
        record.revision += 1
        revision = record.revision
        self._state_changed(thread_id, activity_changed=activity_patch(patches))
        await self._broadcast_change(thread_id, change, list(record.followers))
        return revision

    async def _snapshot(self, thread_id, targets=None):
        record = self._owned[thread_id]
        await self._broadcast_change(
            thread_id,
            {
                "type": "snapshot",
                "revision": record.revision,
                "conversationState": deepcopy(record.state),
            },
            list(record.followers) if targets is None else targets,
        )

    async def _broadcast_change(self, thread_id, change, targets):
        await self.client.broadcast(
            "thread-stream-state-changed",
            {"conversationId": thread_id, "hostId": self.host_id, "change": change},
            target_client_ids=targets,
        )

    async def request_owner(
        self, thread_id, method, params, *, timeout=5, expected_owner_client_id=None
    ):
        self._require_open()
        if method not in FOLLOWER_METHODS:
            raise IpcError("unsupported-operation")
        if not isinstance(params, dict) or params.get("conversationId") != thread_id:
            raise IpcError("conversation-mismatch")
        try:
            async with asyncio.timeout(timeout):
                owner = await self.discover_owner(
                    thread_id,
                    timeout=timeout,
                    expected_owner_client_id=expected_owner_client_id,
                )
                if owner is None:
                    raise IpcError("no-owner")
                self._check_context(method, params, owner.supports_untrusted_app_input)
                if owner.client_id == self.client.client_id:
                    return await self._handle_request(
                        {
                            "method": method,
                            "params": deepcopy(params),
                            "hostId": self.host_id,
                        }
                    )
                response = await self.client.request(
                    method,
                    deepcopy(params),
                    target_client_id=owner.client_id,
                    host_id=None if self.host_id == "local" else self.host_id,
                    timeout=timeout,
                )
                # Operation no-client-found is an error, never owner absence/fallback.
                return response["result"]
        except TimeoutError as exc:
            raise IpcError("timeout") from exc

    async def load_complete_history(
        self, thread_id, *, timeout=305, revision_timeout=30
    ):
        await self.follow(thread_id)
        owner = self.get_owner(thread_id)
        if owner is None:
            raise IpcError("no-owner")
        result = await self.request_owner(
            thread_id,
            "thread-follower-load-complete-history",
            {"conversationId": thread_id},
            timeout=timeout,
            expected_owner_client_id=owner.client_id,
        )
        revision = result.get("revision") if isinstance(result, dict) else None
        if type(revision) is not int or revision < 0:
            raise IpcError("invalid-revision")
        state = await self.wait_revision(
            thread_id,
            revision,
            timeout=revision_timeout,
            expected_owner_client_id=owner.client_id,
        )
        if not history_complete(state):
            raise IpcError("history-incomplete")
        return state

    async def wait_revision(
        self, thread_id, revision, *, timeout=30, expected_owner_client_id=None
    ):
        self._require_open()
        owner = self.get_owner(thread_id)
        if owner is None:
            raise IpcError("owner-unavailable")
        owner_id = expected_owner_client_id or owner.client_id
        if owner.client_id != owner_id:
            raise IpcError("owner-changed")
        current = self.get_revision(thread_id)
        if current is not None and current >= revision:
            return self.get_state(thread_id)
        future = asyncio.get_running_loop().create_future()
        entry = (owner_id, revision, future)
        self._waiters.setdefault(thread_id, set()).add(entry)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise IpcError("revision-timeout") from exc
        finally:
            entries = self._waiters.get(thread_id)
            if entries is not None:
                entries.discard(entry)
                if not entries:
                    self._waiters.pop(thread_id, None)

    def activity_revision(self, thread_id):
        return self._activity.get(thread_id, (None, 0))[1]

    def _state_changed(self, thread_id, *, activity_changed=False):
        state = self.get_state(thread_id)
        if state is not None:
            try:
                signature = activity_signature(state, enumerate_turns(state))
            except (AttributeError, TypeError, ValueError):
                # Keep the peer lossless for malformed/forward native state. The
                # presentation boundary reports unreadable state without logging it.
                signature = ("unreadable", self.get_revision(thread_id))
            previous, _ = self._activity.get(thread_id, (None, 0))
            if activity_changed or signature != previous:
                self._activity_clock += 1
                self._activity[thread_id] = (deepcopy(signature), self._activity_clock)
        # Commit notifications synchronously with the state transition, before
        # awaiting transport I/O. Each waiter/callback receives its own snapshot,
        # even when a later publication finishes sending before this one.
        owner = self.get_owner(thread_id)
        revision = self.get_revision(thread_id)
        for owner_id, expected, future in tuple(self._waiters.get(thread_id, ())):
            if (
                not future.done()
                and owner
                and owner.client_id == owner_id
                and revision >= expected
            ):
                future.set_result(self.get_state(thread_id))
        if self.on_state:
            self._notifications.put_nowait(
                (self.on_state, (thread_id, self.get_state(thread_id)))
            )

    def _fail_waiters(self, thread_id, code):
        for _, _, future in self._waiters.pop(thread_id, ()):
            if not future.done():
                future.set_exception(IpcError(code))

    def _invalidate(self, thread_id, code):
        self._activity.pop(thread_id, None)
        self._fail_waiters(thread_id, code)
        record = self._followed.get(thread_id)
        if record:
            for future in record.snapshots:
                if not future.done():
                    future.set_exception(IpcError(code))
            record.owner = record.state = record.revision = None
        self._queues.pop(thread_id, None)

    def _can_handle(self, envelope):
        params = envelope.get("params")
        if self._closed or not isinstance(params, dict):
            return False
        host = (
            params.get("hostId")
            if envelope.get("method") == "thread-owner-discovery"
            else envelope.get("hostId", "local")
        )
        return host == self.host_id and params.get("conversationId") in self._owned

    async def _handle_request(self, envelope):
        if not self._can_handle(envelope):
            raise IpcError("not-owner")
        method, params = envelope["method"], envelope["params"]
        owned = self._owned[params["conversationId"]]
        if method == "thread-owner-discovery":
            return {"supportsUntrustedAppInput": owned.supports_untrusted_app_input}
        if method not in FOLLOWER_METHODS or self.owner_handler is None:
            raise IpcError("unsupported-operation")
        self._check_context(method, params, owned.supports_untrusted_app_input)
        limit = 300 if method == "thread-follower-load-complete-history" else 5
        try:
            async with asyncio.timeout(limit):
                return await _invoke(self.owner_handler, method, deepcopy(params))
        except TimeoutError as exc:
            raise IpcError("owner-operation-timeout") from exc

    @staticmethod
    def _check_context(method, params, supported):
        if method == "thread-follower-start-turn":
            turn_start = params.get("turnStart", {})
            context = (
                turn_start.get("context") if isinstance(turn_start, dict) else None
            )
            if (
                isinstance(context, dict)
                and context.get("responseItems")
                and not supported
            ):
                raise IpcError("untrusted-app-input-unsupported")

    async def publish_event(self, method, params):
        self._require_open()
        if method not in EVENT_METHODS:
            raise IpcError("unsupported-event")
        thread_id = params.get("conversationId")
        if method == "thread-queued-followups-changed" and not self.is_owner(thread_id):
            raise IpcError("not-owner")
        if "hostId" in params and params["hostId"] != self.host_id:
            raise IpcError("host-mismatch")
        payload = {**deepcopy(params), "hostId": self.host_id}
        if method == "thread-queued-followups-changed":
            self._queues[thread_id] = payload
        await self.client.broadcast(method, payload)

    def _event(self, envelope):
        if self.on_event:
            self._notifications.put_nowait((self.on_event, (deepcopy(envelope),)))

    def _schedule_restore(self, thread_id):
        if thread_id in self._restores or self._closed:
            return

        async def restore():
            try:
                await self.follow(thread_id)
            except IpcError:
                # Restore is read-only; retain intent for the next lifecycle event.
                pass
            finally:
                self._restores.pop(thread_id, None)

        task = asyncio.create_task(restore())
        self._restores[thread_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_broadcast(self, envelope):
        if self._closed:
            return
        method, params = envelope.get("method"), envelope.get("params")
        if not isinstance(params, dict):
            return
        if envelope.get("version", 0) != METHOD_VERSIONS.get(method, 0):
            return
        if method == "ipc-connection-reset":
            if envelope.get("sourceClientId") != self._self_id:
                return
            for thread_id in self._followed:
                self._invalidate(thread_id, "connection-reset")
            for thread_id in self._owned:
                self._fail_waiters(thread_id, "connection-reset")
                self._owned[thread_id].followers.clear()
            self._event(envelope)
            return
        if method == "client-status-changed":
            client_id, status = params.get("clientId"), params.get("status")
            if client_id != envelope.get("sourceClientId"):
                return
            if status == "disconnected":
                for record in self._owned.values():
                    record.followers.discard(client_id)
                for thread_id, followed in self._followed.items():
                    if followed.owner and followed.owner.client_id == client_id:
                        self._invalidate(thread_id, "owner-disconnected")
            elif status == "connected":
                if client_id == self.client.client_id and client_id != self._self_id:
                    self._self_id = client_id
                    for thread_id in self._owned:
                        await self.client.broadcast(
                            "thread-stream-following-status-requested",
                            {"conversationId": thread_id, "hostId": self.host_id},
                        )
                for thread_id in self._followed:
                    self._schedule_restore(thread_id)
            self._event(envelope)
            return
        if params.get("hostId") != self.host_id:
            return
        thread_id = params.get("conversationId")
        if not isinstance(thread_id, str):
            return
        source = envelope.get("sourceClientId")
        if method == "thread-stream-following-changed" and self.is_owner(thread_id):
            record = self._owned[thread_id]
            if params.get("following") is True:
                if not record.followers:
                    record.revision += 1
                    self._state_changed(thread_id)
                record.followers.add(source)
                await self._snapshot(thread_id, [source])
                if thread_id in self._queues:
                    await self.client.broadcast(
                        "thread-queued-followups-changed",
                        self._queues[thread_id],
                        target_client_ids=[source],
                    )
            elif params.get("following") is False:
                record.followers.discard(source)
            return
        if method == "thread-stream-following-status-requested" and self.is_follower(
            thread_id
        ):
            await self._send_follow(thread_id, True, [source])
            owner = self.get_owner(thread_id)
            if owner is None or owner.client_id != source:
                self._schedule_restore(thread_id)
            return
        if method == "thread-stream-state-changed":
            record = self._followed.get(thread_id)
            if not record or not record.owner or record.owner.client_id != source:
                return
            change = params.get("change")
            if not isinstance(change, dict):
                return
            revision = change.get("revision")
            if type(revision) is not int or revision < 0:
                return
            if change.get("type") == "snapshot":
                if not isinstance(change.get("conversationState"), dict):
                    return
                if record.revision is not None and revision < record.revision:
                    return
                record.state = deepcopy(change["conversationState"])
                record.revision = revision
                for future in record.snapshots:
                    if not future.done():
                        future.set_result(self.get_state(thread_id))
            elif change.get("type") == "patches":
                if (
                    record.state is None
                    or change.get("baseRevision") != record.revision
                    or revision <= record.revision
                ):
                    self._schedule_restore(thread_id)
                    return
                try:
                    result = apply_patches(record.state, change.get("patches"))
                except IpcError:
                    self._schedule_restore(thread_id)
                    return
                record.state, record.revision = result, revision
            else:
                return
            self._state_changed(
                thread_id,
                activity_changed=change.get("type") == "patches"
                and activity_patch(change.get("patches", [])),
            )
            return
        if method in EVENT_METHODS:
            if method == "thread-queued-followups-changed":
                owner = self.get_owner(thread_id)
                if owner is None or owner.client_id != source:
                    return
                self._queues[thread_id] = deepcopy(params)
            elif method == "thread-read-state-changed":
                local = self.read_state_context
                if callable(local):
                    local = await _invoke(local)
                if not read_context_matches(local, params.get("context")):
                    return
            self._event(envelope)

    async def _notify_loop(self):
        # User callbacks never block the socket's ordered state application. This
        # also permits callbacks to make owner requests or await later revisions.
        while True:
            callback, args = await self._notifications.get()
            try:
                await _invoke(callback, *args)
            except Exception:  # noqa: BLE001 - isolate application callbacks without logging private data
                logger.warning("Codex coordination callback failed")
            finally:
                self._notifications.task_done()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for thread_id in self._followed:
            self._invalidate(thread_id, "disposed")
        for thread_id in self._owned:
            self._fail_waiters(thread_id, "disposed")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            async with asyncio.timeout(0.25):
                for thread_id in self._followed:
                    await self._send_follow(thread_id, False)
        except (IpcError, TimeoutError):
            pass
        finally:
            for remove in self._removers:
                remove()
            await self.client.close()
            if self._notification_task:
                self._notification_task.cancel()
                await asyncio.gather(self._notification_task, return_exceptions=True)
            self._owned.clear()
            self._followed.clear()
            self._follow_locks.clear()
            self._queues.clear()
            self._activity.clear()
