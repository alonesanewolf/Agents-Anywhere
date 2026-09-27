"""Bounded explicit-view follows. Inventory reads deliberately bypass this seam."""

import asyncio
from collections import Counter, OrderedDict
from functools import wraps


def guarded_mutation(function):
    @wraps(function)
    async def invoke(runtime, *args, **kwargs):
        session_id = kwargs.get("session_id") or args[0]
        observers = runtime._observers
        cached = observers.states.get(session_id)
        if (
            cached is not None
            and cached.metadata.get("codexCoordination", {}).get("available") is False
        ):
            raise ValueError(
                "Codex owner state is unavailable; refresh the session before mutating"
            )
        observers.busy[session_id] += 1
        try:
            return await function(runtime, *args, **kwargs)
        finally:
            observers.busy[session_id] -= 1
            if not observers.busy[session_id]:
                del observers.busy[session_id]

    return invoke


class CodexSessionObservers:
    # Leave eight of the peer's 128 follow slots for transient inventory reads.
    LIMIT = 120

    def __init__(self, client, states, notices, active_turn_ids, start):
        self.client, self.states, self.notices = client, states, notices
        self.active_turn_ids, self.start = active_turn_ids, start
        self.lock = asyncio.Lock()
        self.views = OrderedDict()
        self.busy = Counter()

    def clear(self):
        self.views.clear()
        self.busy.clear()

    async def prepare(self, session_id, thread_id):
        attach = getattr(self.client, "attach_thread", None)
        if not thread_id or not callable(attach):
            return
        await self.start()
        async with self.lock:
            if thread_id not in self.views and len(self.views) >= self.LIMIT:
                for old_thread, old_session in tuple(self.views.items()):
                    can_detach = self.client.is_follower(
                        old_thread
                    ) or not self.client.has_canonical_authority(old_thread)
                    if (
                        can_detach
                        and old_session not in self.active_turn_ids
                        and not self.busy[old_session]
                        and not self.notices.open_blocking_for_session(old_session)
                    ):
                        await self.client.detach_thread(old_thread)
                        self.views.pop(old_thread)
                        break
                else:
                    raise RuntimeError(
                        "All observed Codex sessions are busy; observer capacity reached"
                    )
            if self.states.get(session_id) is None:
                await self.states.update(session_id, thread_id, status="idle")
            await attach(thread_id)
            self.views[thread_id] = session_id
            self.views.move_to_end(thread_id)
            await self.client.refresh_state(thread_id, force=True)
