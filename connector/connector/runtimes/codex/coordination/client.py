"""Runtime facade coordinating ownership while the official SDK owns native I/O."""

import asyncio
from collections import defaultdict
from copy import deepcopy

from connector.runtimes.codex.sdk.client import codex_turn_user_input_wire
from connector.runtimes.codex.sdk.runtime_client import (
    CodexCompactResult,
    CodexThreadReadResult,
    CodexThreadResult,
    CodexThreadTurnsResult,
    CodexTurnResult,
)

from .journal import CoordinationJournal
from .operations import OwnerOperations
from .projection import native_to_state, presentation, state_to_native
from .queue import execute_head
from .reducer import exact_id, merge_settings, reduce_event
from .requests import REQUEST_ROUTES, ResponseContexts, validate_response
from .state import history_complete


class CoordinatedCodexClient:
    def __init__(self, sdk_client, peer, *, kv_store, namespace):
        self.sdk = sdk_client
        self.peer = peer
        self.journal = CoordinationJournal(kv_store, namespace)
        self.operations = OwnerOperations(self.sdk, self.peer, self.journal)
        self.contexts = ResponseContexts()
        self.handler = None
        self.attached = set()
        self.owned = set()
        self.acquiring = {}
        self.locks = defaultdict(asyncio.Lock)
        self.last_emitted = {}
        self.queue_tasks = {}
        self.queue_wakes = set()
        self.tasks = set()
        self.goal_support = self.operations.goal_support
        self.goal_epochs = self.operations.goal_epochs
        self.generation = 0
        self.closed = False
        self.remover = None

    async def start(self, handler):
        self.handler = handler
        self.peer.on_state = self._on_state
        self.peer.on_event = self._on_event
        self.peer.owner_handler = self._owner_operation
        self.sdk.set_native_event_handler(self._native_event)
        await self.peer.start()
        try:
            await self.sdk.start(self._sdk_event)
        except BaseException:
            await self.peer.close()
            raise

    async def stop(self):
        self.closed = True
        self.contexts.clear()
        if self.remover:
            self.remover()
        for task in self.tasks | set(self.queue_tasks.values()):
            task.cancel()
        await asyncio.gather(
            *self.tasks, *self.queue_tasks.values(), return_exceptions=True
        )
        await self.sdk.stop()
        await self.peer.close()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return task

    async def _sdk_event(self, message):
        # Coordinated thread updates have exactly one authority and arrive through
        # raw snapshots. Keep unrelated native/global events on the existing path.
        if isinstance(message, dict):
            thread_id = message.get("params", {}).get("threadId")
        else:
            thread_id = getattr(message, "thread_id", None)
        if thread_id is None and self.handler and not self.closed:
            await self.handler(message)

    async def _native_event(self, message, generation):
        if self.closed or generation != self.sdk.native_generation:
            return
        if message["method"] == "native/disconnected":
            self.generation += 1
            self.contexts.clear()
            for thread_id in tuple(self.owned):
                await self.peer.release(thread_id)
                await self.refresh_state(thread_id, force=True)
            self.owned.clear()
            self.goal_support.clear()
            return
        params = message.get("params") or {}
        thread_id = params.get("threadId", (params.get("thread") or {}).get("id"))
        if "id" in message and message["method"] not in REQUEST_ROUTES.values():
            await self.sdk.reject_native_request(
                message["id"],
                "unsupported native server request method",
                generation=generation,
            )
            return
        if thread_id in self.acquiring:
            self.acquiring[thread_id].append(deepcopy(message))
            return
        if not thread_id or not self.peer.is_owner(thread_id):
            if "id" in message:
                await self.sdk.reject_native_request(
                    message["id"],
                    "native request has no local owner",
                    generation=generation,
                )
            return
        if "id" in message:
            self.contexts.new_request(thread_id, message["id"], message["method"])
        if message["method"] in {"thread/goal/updated", "thread/goal/cleared"}:
            self.goal_support[thread_id] = True
            self.goal_epochs[thread_id] += 1
        state = reduce_event(self.peer.get_state(thread_id), message)
        await self.peer.publish_state(thread_id, state)
        if not self.operations.mutation_locks[thread_id].locked():
            await self.operations.reconcile(thread_id)
        self._kick_queue(thread_id)

    async def _on_state(self, thread_id, _stale_payload):
        await self.refresh_state(thread_id)

    async def _on_event(self, envelope):
        if self.closed:
            return
        if envelope.get("method") in ("client-status-changed", "ipc-connection-reset"):
            await self._lifecycle(envelope)
        thread_id = envelope.get("params", {}).get("conversationId")
        if thread_id:
            await self.refresh_state(thread_id, force=True)

    async def _lifecycle(self, envelope):
        if self.closed:
            return
        method = envelope.get("method")
        if method not in ("client-status-changed", "ipc-connection-reset"):
            return
        params = envelope.get("params", {})
        source = envelope.get("sourceClientId")
        if method == "client-status-changed" and source != params.get("clientId"):
            return
        if method == "ipc-connection-reset" and source != self.peer.client.client_id:
            return
        self.generation += 1
        self.contexts.clear()

        # Peer callbacks are sequential but handler registration order is not
        # significant: defer the read until this broadcast finishes applying.
        async def refresh():
            await asyncio.sleep(0)
            for thread_id in self.attached | self.owned:
                await self.refresh_state(thread_id, force=True)

        self._spawn(refresh())

    def _snapshot(self, thread_id):
        # All getters are synchronous. Never mix queued payload with live metadata.
        state = self.peer.get_state(thread_id)
        owner = self.peer.get_owner(thread_id)
        revision = self.peer.get_revision(thread_id)
        role = (
            "owner"
            if self.peer.is_owner(thread_id)
            else "follower"
            if self.peer.is_follower(thread_id)
            else "none"
        )
        source = (
            owner.client_id if owner else None,
            self.generation,
            self.sdk.native_generation,
        )
        return state, owner, revision, role, source

    async def refresh_state(self, thread_id, *, force=False):
        if self.closed or self.handler is None:
            return
        state, owner, revision, role, source = self._snapshot(thread_id)
        identity = (source, revision, role)
        if not force and self.last_emitted.get(thread_id) == identity:
            return
        self.last_emitted[thread_id] = identity
        if state is None or owner is None:
            self.contexts.clear(thread_id)
            params = {
                "threadId": thread_id,
                "thread": None,
                "canonicalComplete": False,
                "requests": [],
                "requestContexts": [],
                "coordination": {
                    "role": role,
                    "ownerClientId": None,
                    "revision": None,
                    "generation": source[1:],
                },
                "presentation": None,
                "capabilities": self.capabilities(thread_id),
            }
        else:
            requests = state.get("requests", [])
            params = {
                "threadId": thread_id,
                "thread": state_to_native(state),
                "canonicalComplete": history_complete(state),
                "requests": deepcopy(requests),
                "requestContexts": self.contexts.present(thread_id, source, requests),
                "coordination": {
                    "role": role,
                    "ownerClientId": owner.client_id,
                    "revision": revision,
                    "generation": source[1:],
                },
                "presentation": presentation(state),
                "capabilities": self.capabilities(thread_id),
            }
        await self.handler({"method": "coordination/state", "params": params})

    def is_follower(self, thread_id):
        return self.peer.is_follower(thread_id)

    def has_canonical_authority(self, thread_id):
        return self.peer.is_owner(thread_id) or self.peer.is_follower(thread_id)

    def capabilities(self, thread_id):
        return {
            "role": "owner"
            if self.peer.is_owner(thread_id)
            else "follower"
            if self.peer.is_follower(thread_id)
            else "unattached",
            "nativeVersion": self.sdk.native_runtime_info().get("version"),
            "goalControl": self.peer.is_owner(thread_id)
            and self.command_capabilities(thread_id)["nativeControls"]
            and self.goal_support.get(thread_id, False),
            "userSessionStop": self.peer.is_owner(thread_id)
            or self.peer.is_follower(thread_id),
            "supportsUntrustedAppInput": False,
            "modelCatalogScope": "local-sdk",
            "unsupportedContexts": [
                "writing blocks",
                "editor-only attachments without native input",
                "permission selection preparation",
                "MCP model-context attachments",
                "special MCP authentication/verification",
            ],
            "stopCleanup": {
                "nativeTurn": True,
                "backgroundTerminals": False,
                "nodeRepl": False,
                "subagentDescendants": False,
            },
        }

    async def attach_thread(self, thread_id):
        async with self.locks[thread_id]:
            self.attached.add(thread_id)
            if self.peer.is_owner(thread_id):
                return self.peer.get_state(thread_id)
            try:
                return await self.peer.follow(thread_id)
            except BaseException:
                self.attached.discard(thread_id)
                raise

    async def detach_thread(self, thread_id):
        async with self.locks[thread_id]:
            self.attached.discard(thread_id)
            self.contexts.clear(thread_id)
            self.last_emitted.pop(thread_id, None)
            if self.peer.is_follower(thread_id):
                await self.peer.unfollow(thread_id)

    async def _acquire(self, thread_id):
        async with self.locks[thread_id]:
            if self.peer.is_owner(thread_id):
                return
            state = await self.peer.follow(thread_id)
            if state is not None:
                self.attached.add(thread_id)
                return
            # Follow confirmed absence; repeat discovery just before native resume.
            owner = await self.peer.discover_owner(thread_id)
            if owner is not None:
                await self.peer.follow(thread_id)
                self.attached.add(thread_id)
                return
            self.acquiring[thread_id] = []
            try:
                result = await self.sdk.native_thread_resume(thread_id)
                state = native_to_state(
                    result["thread"], complete=False, host_id=self.peer.host_id
                )
                merge_settings(
                    state,
                    {
                        k: deepcopy(v)
                        for k, v in result.items()
                        if k
                        in {
                            "model",
                            "approvalPolicy",
                            "approvalsReviewer",
                            "serviceTier",
                            "cwd",
                        }
                    },
                )
                if "reasoningEffort" in result:
                    merge_settings(state, {"effort": result["reasoningEffort"]})
                for message in self.acquiring[thread_id]:
                    state = reduce_event(state, message)
                await self.peer.claim(
                    thread_id, state, supports_untrusted_app_input=False
                )
                self.owned.add(thread_id)
            finally:
                self.acquiring.pop(thread_id, None)

    async def _route(self, thread_id, suffix, params):
        await self._acquire(thread_id)
        method = "thread-follower-" + suffix
        params = {**deepcopy(params), "conversationId": thread_id}
        if self.peer.is_owner(thread_id):
            return await self._owner_operation(method, params)
        return await self.peer.request_owner(thread_id, method, params)

    async def _owner_operation(self, method, params):
        result = await self.operations.handle(method, params)
        self._kick_queue(params["conversationId"])
        return result

    def _kick_queue(self, thread_id):
        if (
            self.closed
            or not self.peer.is_owner(thread_id)
            or not self.journal.queue(thread_id)
        ):
            return
        if thread_id in self.queue_tasks:
            self.queue_wakes.add(thread_id)
            return
        task = asyncio.create_task(execute_head(self.operations, thread_id))
        self.queue_tasks[thread_id] = task

        def done(completed):
            self.queue_tasks.pop(thread_id, None)
            progressed = (
                not completed.cancelled()
                and completed.exception() is None
                and completed.result() is True
            )
            wake = thread_id in self.queue_wakes
            self.queue_wakes.discard(thread_id)
            if progressed or wake:
                # One bounded head check per progress signal. Active/paused/failed
                # guards return False and stop without needing another notification.
                self._kick_queue(thread_id)

        task.add_done_callback(done)

    async def list_models(self):
        return await self.sdk.list_models()

    async def list_threads(self, limit=100, cursor=None, archived=None):
        return await self.sdk.list_threads(
            limit=limit, cursor=cursor, archived=archived
        )

    async def read_thread(self, thread_id, include_turns=True):
        async with self.locks[thread_id]:
            state = self.peer.get_state(thread_id)
            temporary = thread_id not in self.attached and not self.peer.is_owner(
                thread_id
            )
            try:
                if state is None:
                    state = await self.peer.follow(thread_id)
                if state is not None:
                    thread = state_to_native(state)
                    if not include_turns:
                        thread["turns"] = []
                    return CodexThreadReadResult(
                        thread=thread,
                        canonical_complete=history_complete(state),
                        coordination_role="owner"
                        if self.peer.is_owner(thread_id)
                        else "follower",
                    )
                raw = await self.sdk.native_request(
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": include_turns},
                )
                return CodexThreadReadResult(thread=raw["thread"])
            finally:
                if temporary and self.peer.is_follower(thread_id):
                    await self.peer.unfollow(thread_id)
                    self.contexts.clear(thread_id)

    async def list_thread_turns(self, thread_id):
        state = await self.load_complete_history(thread_id)
        return CodexThreadTurnsResult(turns=tuple(state_to_native(state)["turns"]))

    async def load_complete_history(self, thread_id):
        from .history import read_complete

        async with self.locks[thread_id]:
            temporary = thread_id not in self.attached and not self.peer.is_owner(
                thread_id
            )
            try:
                if self.peer.is_owner(thread_id):
                    await self.operations.handle(
                        "thread-follower-load-complete-history",
                        {"conversationId": thread_id},
                    )
                    return self.peer.get_state(thread_id)
                state = await self.peer.follow(thread_id)
                if state is not None:
                    return await self.peer.load_complete_history(thread_id)
                return await read_complete(
                    self.sdk.native_request, thread_id, host_id=self.peer.host_id
                )
            finally:
                if temporary and self.peer.is_follower(thread_id):
                    await self.peer.unfollow(thread_id)
                    self.contexts.clear(thread_id)

    async def owner_operation(self, thread_id, method, params):
        """Explicit rich operations for runtime commands; responses require tokens."""
        from .peer import FOLLOWER_METHODS

        if method not in FOLLOWER_METHODS:
            raise ValueError("unsupported follower operation")
        if method.removeprefix("thread-follower-") in REQUEST_ROUTES:
            raise ValueError("approval responses require respond_to_request context")
        if params.get("conversationId", thread_id) != thread_id:
            raise ValueError("conversation identity mismatch")
        return await self._route(
            thread_id, method.removeprefix("thread-follower-"), params
        )

    async def command_owner_operation(self, thread_id, method, params):
        from .peer import FOLLOWER_METHODS

        if (
            method not in FOLLOWER_METHODS
            or method.removeprefix("thread-follower-") in REQUEST_ROUTES
        ):
            raise ValueError("unsupported command control")
        params = {**deepcopy(params), "conversationId": thread_id}
        if self.peer.is_owner(thread_id):
            return await self._owner_operation(method, params)
        if not self.peer.is_follower(thread_id):
            raise ValueError("command control requires an existing owner")
        owner = self.peer.get_owner(thread_id)
        if owner is None:
            raise ValueError("command control requires an existing owner")
        return await self.peer.request_owner(
            thread_id, method, params, expected_owner_client_id=owner.client_id
        )

    async def start_thread(self, request):
        result = await self.sdk.native_thread_start(request)
        thread_id = result["thread"]["id"]
        state = native_to_state(
            result["thread"], complete=True, host_id=self.peer.host_id
        )
        await self.peer.claim(thread_id, state)
        self.owned.add(thread_id)
        return CodexThreadResult(thread_id=thread_id, payload=result)

    async def start_turn(self, request):
        native = {
            "threadId": request.thread_id,
            "input": codex_turn_user_input_wire(request),
        }
        for source, target in (
            ("client_message_id", "clientUserMessageId"),
            ("model", "model"),
            ("effort", "effort"),
            ("approval_policy", "approvalPolicy"),
            ("approvals_reviewer", "approvalsReviewer"),
        ):
            if getattr(request, source) is not None:
                native[target] = getattr(request, source)
        from connector.runtimes.codex.sdk.client import codex_approval_settings

        policy, reviewer = codex_approval_settings(
            request.approval_policy, request.approvals_reviewer
        )
        if policy is not None:
            native["approvalPolicy"] = policy.model_dump(mode="json")
        if reviewer is not None:
            native["approvalsReviewer"] = reviewer.value
        if request.sandbox is not None:
            from connector.runtimes.codex.sdk.client import codex_turn_start_params

            sandbox = codex_turn_start_params(request).model_dump(
                by_alias=True, exclude_none=True, mode="json"
            )
            if "sandboxPolicy" in sandbox:
                native["sandboxPolicy"] = sandbox["sandboxPolicy"]
        result = await self._route(
            request.thread_id,
            "start-turn",
            {
                "turnStart": {
                    "request": native,
                    "context": {
                        "inheritThreadSettings": request.model is None
                        and request.effort is None
                    },
                }
            },
        )
        return CodexTurnResult(
            turn_id=result["result"]["turn"]["id"], payload=result["result"]
        )

    async def steer_turn(self, request):
        result = await self._route(
            request.thread_id,
            "steer-turn",
            {
                "input": [{"type": "text", "text": request.content}],
                "clientUserMessageId": request.client_message_id,
                "expectedTurnId": request.turn_id,
            },
        )
        return CodexTurnResult(
            turn_id=result["result"]["turnId"], payload=result["result"]
        )

    async def interrupt_turn(self, request):
        result = await self._route(
            request.thread_id,
            "interrupt-turn",
            {"expectedTurnId": request.turn_id, "mode": "system"},
        )
        return CodexTurnResult(turn_id=result["interruptedTurnId"], payload=result)

    async def compact_thread(self, thread_id):
        return CodexCompactResult(
            payload=await self._route(thread_id, "compact-thread", {})
        )

    async def respond(self, request_id, result=None):
        # Deliberately not the coordinated API. Caller must retain responseContext.
        return await self.sdk.respond(request_id, result)

    async def respond_to_request(self, response_context, result):
        thread_id, source, _id_type, request_id, method = self.contexts.peek(
            response_context
        )
        state, owner, _, _, current = self._snapshot(thread_id)
        if state is None or owner is None or source != current:
            raise ValueError("stale response context authority")
        request = next(
            (
                r
                for r in state.get("requests", [])
                if exact_id(r.get("id"), request_id)
                and r.get("method") == method
                and r.get("params", {}).get("threadId") == thread_id
            ),
            None,
        )
        if request is None:
            raise ValueError("stale response context request")
        validate_response(request, result)
        suffix = next(
            (key for key, value in REQUEST_ROUTES.items() if value == method), None
        )
        if suffix is None:
            raise ValueError("unsupported native response method")
        params = {"conversationId": thread_id, "requestId": request_id}
        if suffix in ("command-approval-decision", "file-approval-decision"):
            params["decision"] = deepcopy(result["decision"])
        else:
            params["response"] = deepcopy(dict(result))
        # No await between authority validation, local validation and consumption.
        # Once dispatch starts, errors/timeouts cannot safely make this token reusable.
        self.contexts.take(response_context)
        if self.peer.is_owner(thread_id):
            await self.operations.handle("thread-follower-" + suffix, params)
        else:
            await self.peer.request_owner(
                thread_id,
                "thread-follower-" + suffix,
                params,
                expected_owner_client_id=owner.client_id,
            )

    async def reconcile_thread(self, thread_id):
        if not self.peer.is_owner(thread_id):
            raise ValueError("reconciliation requires AA ownership")
        await self.load_complete_history(thread_id)
        return await self.operations.reconcile(thread_id)

    def command_capabilities(self, thread_id):
        import re

        from packaging.version import InvalidVersion, Version

        info = self.native_runtime_info()
        try:
            verified = Version(
                re.sub(r"-alpha\.(\d+)(?:\.\d+)*", r"a\1", info.get("version") or "0")
            ) >= Version("0.155.1")
        except InvalidVersion:
            verified = False
        return {
            "role": "owner"
            if self.peer.is_owner(thread_id)
            else "follower"
            if self.peer.is_follower(thread_id)
            else "none",
            "coordinated": True,
            "nativeControls": verified and info.get("rawEvents") is True,
            "nativeVersion": info.get("version"),
            "goalSupported": self.goal_support.get(thread_id, False),
            "goalObservationOwned": True,
        }

    async def stop_session(self, thread_id):
        # Explicit user stop never claims a missing owner or retargets a turn.
        params = {"conversationId": thread_id, "mode": "user-stop"}
        if self.peer.is_owner(thread_id):
            result = await self._owner_operation(
                "thread-follower-interrupt-turn", params
            )
        else:
            if not self.peer.is_follower(thread_id):
                raise ValueError("session stop requires an existing owner")
            result = await self.peer.request_owner(
                thread_id, "thread-follower-interrupt-turn", params
            )
        # Project current canonical state before returning the acknowledgement;
        # queued state notifications need not have run at this point.
        await self.refresh_state(thread_id, force=True)
        return result

    async def observe_goal(self, thread_id, goal):
        return await self.operations.observe_goal(thread_id, goal)

    async def project_goal(self, thread_id):
        # The canonical projector owns coordinated AA presentation. Callers must
        # not subsequently repaint a native reply that predates this await.
        await self.refresh_state(thread_id, force=True)
        return deepcopy(self.operations.state(thread_id).get("threadGoal"))

    async def native_request(self, method, params):
        thread_id = params.get("threadId")
        if not thread_id or not self.peer.is_owner(thread_id):
            raise ValueError("native control requires an already AA-owned thread")
        token = self.operations.epoch.set(self.sdk.native_generation)
        try:
            async with (
                self.operations.settings_locks[thread_id],
                self.operations.mutation_locks[thread_id],
            ):
                if method in {
                    "thread/goal/get",
                    "thread/goal/set",
                    "thread/goal/clear",
                }:
                    return await self.operations.goal_request(thread_id, method, params)
                return await self.operations.call(thread_id, method, params)
        finally:
            self.operations.epoch.reset(token)

    def native_runtime_info(self):
        return self.sdk.native_runtime_info()
