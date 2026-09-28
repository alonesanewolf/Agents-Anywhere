"""Bounded explicit-view follows. Inventory reads deliberately bypass this seam."""

import asyncio
from collections import Counter, OrderedDict
from dataclasses import dataclass
from functools import wraps

from connector.runtime_protocol.errors import RuntimeProtocolError
from connector.runtimes.codex.coordination.wire import IpcError

# One 12s owner discovery plus 5s for locks/startup/read/projection/hydration.
# This is a total allocation, not a fresh timeout for each step.
PREPARE_TIMEOUT_SECONDS = 17


class CodexViewTimeout(RuntimeProtocolError):
    code = "codex_view_timeout"
    retryable = True


class CodexViewChanged(RuntimeProtocolError):
    code = "codex_view_changed"
    retryable = True


def guarded_mutation(function=None, *, allow_unavailable=False):
    if function is None:
        return lambda decorated: guarded_mutation(
            decorated, allow_unavailable=allow_unavailable
        )

    @wraps(function)
    async def invoke(runtime, *args, **kwargs):
        session_id = kwargs.get("session_id") or args[0]
        observers = runtime._observers
        cached = observers.states.get(session_id)
        if (
            not allow_unavailable
            and cached is not None
            and cached.metadata.get("codexCoordination", {}).get("available") is False
        ):
            raise ValueError(
                "Codex owner state is unavailable; refresh the session before mutating"
            )
        # clear() replaces the generation's counter; old finally blocks cannot
        # decrement a new generation's mutation pin.
        busy = observers.busy
        busy[session_id] += 1
        try:
            return await function(runtime, *args, **kwargs)
        finally:
            busy[session_id] -= 1
            if not busy[session_id]:
                del busy[session_id]

    return invoke


@dataclass(eq=False)
class Preparation:
    session_id: str
    generation: int
    deadline: float
    victim: tuple[str, str] | None = None
    task: asyncio.Task | None = None


class CodexSessionObservers:
    # Leave eight of the peer's 128 follow slots for transient inventory reads.
    LIMIT = 120

    def __init__(
        self,
        client,
        states,
        notices,
        active_turn_ids,
        start,
        read_state,
        prepare_state=None,
    ):
        self.client, self.states, self.notices = client, states, notices
        self.active_turn_ids, self.start = active_turn_ids, start
        self.read_state, self.prepare_state = read_state, prepare_state
        self.hydrate = None
        self.lock = asyncio.Lock()
        self.views = OrderedDict()
        self.busy = Counter()
        self.inflight = {}
        self.evicting = {}
        self.generation = 0
        self.closed = False

    def clear(self):
        tasks = [entry.task for entry in self.inflight.values()]
        self.generation += 1
        self.views = OrderedDict()
        self.busy = Counter()
        self.inflight = {}
        self.evicting = {}
        for task in tasks:
            task.cancel()
        return tasks

    async def close(self):
        self.closed = True
        await asyncio.gather(*self.clear(), return_exceptions=True)

    def _can_detach(self, thread_id, session_id):
        return (
            (
                self.client.is_follower(thread_id)
                or not self.client.has_canonical_authority(thread_id)
            )
            and session_id not in self.active_turn_ids
            and not self.busy[session_id]
            and not self.notices.open_blocking_for_session(session_id)
            and not self.notices.unresolved_contexts_for_session(session_id)
        )

    def _current(self, thread_id, entry):
        return (
            not self.closed
            and self.generation == entry.generation
            and self.inflight.get(thread_id) is entry
        )

    async def prepare(self, session_id, thread_id):
        if not thread_id or not callable(getattr(self.client, "attach_thread", None)):
            return
        if self.closed:
            raise CodexViewChanged("Codex observer is closed")
        deadline = asyncio.get_running_loop().time() + PREPARE_TIMEOUT_SECONDS
        async with self.lock:
            if thread_id in self.evicting:
                raise CodexViewChanged("Codex view is being released; refresh it")
            entry = self.inflight.get(thread_id)
            if entry is None:
                entry = Preparation(session_id, self.generation, deadline)
                reserved = set(self.views) | set(self.inflight)
                if (
                    thread_id not in reserved
                    and len(reserved) - len(self.evicting) >= self.LIMIT
                ):
                    for old_thread, old_session in self.views.items():
                        if (
                            old_thread not in self.inflight
                            and old_thread not in self.evicting
                            and self._can_detach(old_thread, old_session)
                        ):
                            entry.victim = old_thread, old_session
                            self.evicting[old_thread] = entry
                            break
                    else:
                        raise RuntimeError(
                            "All observed Codex sessions are busy; observer capacity reached"
                        )
                self.inflight[thread_id] = entry
                entry.task = asyncio.create_task(self._prepare(thread_id, entry))
                entry.task.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
            elif entry.session_id != session_id:
                raise CodexViewChanged("Codex view session identity changed")
        # Caller cancellation cannot cancel another caller's shared preparation.
        await asyncio.shield(entry.task)

    async def _prepare(self, thread_id, entry):
        try:
            async with asyncio.timeout_at(entry.deadline):
                await self.start()
                if entry.victim:
                    old_thread, old_session = entry.victim

                    def can_detach():
                        return (
                            self._current(thread_id, entry)
                            and self.evicting.get(old_thread) is entry
                            and self.views.get(old_thread) == old_session
                            and self._can_detach(old_thread, old_session)
                        )

                    detach = getattr(self.client, "detach_view", None)
                    if callable(detach):
                        detached = await detach(old_thread, can_detach)
                    else:
                        detached = can_detach()
                        if detached:
                            await self.client.detach_thread(old_thread)
                    if not detached or not self._current(thread_id, entry):
                        raise CodexViewChanged("Codex view became busy during release")
                    self.views.pop(old_thread, None)
                    self.evicting.pop(old_thread, None)
                if not self._current(thread_id, entry):
                    raise CodexViewChanged("Codex observer generation changed")
                self.views[thread_id] = entry.session_id
                self.views.move_to_end(thread_id)
                cached = self.states.get(entry.session_id)
                # Bind canonical events to the AA identity. Existing observed
                # status/selections may remain visible, but a failed preparation
                # must leave the retained slot explicitly unavailable.
                await self.states.update(
                    entry.session_id,
                    thread_id,
                    status=cached.status if cached is not None else "blocked",
                    metadata={
                        "source": "codex.view.preparing",
                        "codexCoordination": {"available": False},
                    },
                )
                if callable(getattr(self.client, "prepare_view", None)):
                    state, token = await self.prepare_state(entry.session_id, thread_id)
                    if not self._current(
                        thread_id, entry
                    ) or not self.client.view_is_current(thread_id, token):
                        raise CodexViewChanged(
                            "Codex authority changed during passive read"
                        )
                    if state.metadata["codexCoordination"]["role"] != "unattached":
                        await self.client.refresh_state(
                            thread_id,
                            force=True,
                            is_current=lambda: (
                                self._current(thread_id, entry)
                                and self.client.view_is_current(thread_id, token)
                            ),
                        )
                    else:
                        await self.states.update(
                            entry.session_id,
                            thread_id,
                            status=state.status,
                            selections=state.selections,
                            metadata=state.metadata,
                        )
                else:
                    # Compatibility for injected clients predating passive prepare.
                    await self.client.attach_thread(thread_id)
                    if self.client.has_canonical_authority(thread_id):
                        await self.client.refresh_state(thread_id, force=True)
                    else:
                        state = await self.read_state(entry.session_id, thread_id)
                        if not self._current(thread_id, entry):
                            raise CodexViewChanged("Codex observer generation changed")
                        await self.states.update(
                            entry.session_id,
                            thread_id,
                            status=state.status,
                            selections=state.selections,
                            metadata=state.metadata,
                        )
                if self._current(thread_id, entry) and self.hydrate:
                    await self.hydrate(entry.session_id, thread_id)
        except IpcError as exc:
            if exc.code == "codex_view_changed":
                raise CodexViewChanged(
                    "Codex authority changed during passive read"
                ) from exc
            raise
        except TimeoutError as exc:
            raise CodexViewTimeout("Codex passive view preparation timed out") from exc
        finally:
            if self._current(thread_id, entry):
                self.inflight.pop(thread_id, None)
                if entry.victim and self.evicting.get(entry.victim[0]) is entry:
                    self.evicting.pop(entry.victim[0], None)
