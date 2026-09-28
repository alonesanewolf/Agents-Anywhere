"""Installed-router timing: isolated framed sockets and fake native I/O only."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from connector.core.json_kv import JsonKeyValueStore
from connector.runtimes.codex.coordination import peer as peer_module
from connector.runtimes.codex.coordination import router as router_module
from connector.runtimes.codex.coordination import transport as transport_module
from connector.runtimes.codex.coordination.client import CoordinatedCodexClient
from connector.runtimes.codex.coordination.peer import CoordinationPeer
from connector.runtimes.codex.coordination.router import CoordinationRouter
from connector.runtimes.codex.coordination.transport import CoordinationClient
from connector.runtimes.codex.coordination.wire import IpcError
from connector.runtimes.codex.sdk.runtime_client import CodexStartTurnRequest

SCALE = 0.04
THREAD = "owner-absence-test"
SETTINGS = {
    "model": "gpt-6-luna",
    "effort": "low",
    "approvalPolicy": "on-request",
    "approvalsReviewer": "user",
    "sandboxPolicy": {"type": "readOnly"},
}


class ScaledDeadlines:
    """Scale only test-owned IPC deadlines, preserving default arguments/ratios."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def timeout(self, delay):
        return asyncio.timeout(None if delay is None else delay * SCALE)


@pytest.fixture(autouse=True)
def scaled_deadlines(monkeypatch):
    for module in (peer_module, router_module, transport_module):
        monkeypatch.setattr(module, "asyncio", ScaledDeadlines())


class ObservedRouter(CoordinationRouter):
    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.negative_responses = []
        self.discovery_count = 0
        self.before_discovery = None
        self.last_error = None

    async def _exchange(self, peer, message, collection, timeout):
        response = await super()._exchange(peer, message, collection, timeout)
        if collection is self._discoveries and response.get("response") == {
            "canHandle": False
        }:
            self.negative_responses.append(response)
        return response

    async def _route(self, source, message):
        if message.get("method") == "thread-owner-discovery":
            self.discovery_count += 1
            if self.before_discovery:
                error = await self.before_discovery(self.discovery_count)
                if error:
                    await self._error(source, message["requestId"], error)
                    return
        await super()._route(source, message)

    async def _error(self, peer, request_id, code):
        self.last_error = code
        await super()._error(peer, request_id, code)


class SilentCandidate(CoordinationClient):
    async def _dispatch(self, message, writer):
        if message.get("type") == "client-discovery-request":
            return  # Still connected/registered, but never answers discovery.
        await super()._dispatch(message, writer)


class FakeNative:
    native_generation = 1

    def __init__(self, home, *, cost=0, start_gate=None):
        self.calls, self.cost, self.start_gate = [], cost, start_gate
        self.started = asyncio.Event()
        from codex_resume_fixture import RolloutNative

        self.persisted = RolloutNative(home)
        self.persisted.settings["permission_profile"]["file_system"]["entries"] = (
            self.persisted.settings["permission_profile"]["file_system"]["entries"][:1]
        )
        self.persisted.records[1] = self.persisted.applied()
        self.persisted.sandbox = {"type": "read-only"}
        self.persisted.records[-1] = self.persisted.context()
        self.persisted.save()

    async def native_thread_resume(self, thread_id, *, settings=None):
        self.calls.append(("thread/resume", {"threadId": thread_id}))
        await asyncio.sleep(self.cost)
        return await self.persisted.sdk.native_thread_resume(
            thread_id, settings=settings
        )

    async def native_request(self, method, params):
        if method == "thread/read":
            return {"thread": self.persisted.thread()}
        assert method == "turn/start", method
        self.calls.append((method, deepcopy(params)))
        self.started.set()
        if self.start_gate:
            await self.start_gate.wait()
        await asyncio.sleep(self.cost)
        return {"turn": {"id": "new-turn", "status": "inProgress", "items": []}}

    def native_runtime_info(self):
        return {"version": "0.155.1"}


@asynccontextmanager
async def network(*, silent=True, native_cost=0, start_gate=None):
    # Short private /tmp path for macOS Unix socket limits; no default home used.
    with TemporaryDirectory(prefix="aa-e9-", dir="/tmp") as temporary:
        home = Path(temporary)
        endpoint = home / "ipc.sock"
        router = ObservedRouter(endpoint)
        await router.start()
        peers, clients = [], []
        try:
            for name in ("caller", "negative", "possible-owner"):
                transport = CoordinationClient(
                    home, endpoint=endpoint, client_type=name, start_router=False
                )
                peer = CoordinationPeer(transport)
                peers.append(peer)
                await peer.start()
            if silent:
                transport = SilentCandidate(home, endpoint=endpoint, start_router=False)
                clients.append(transport)
                await transport.start()
                await transport.wait_initialized()
            caller, _, owner = peers
            sdk = FakeNative(home, cost=native_cost, start_gate=start_gate)
            facade = CoordinatedCodexClient(
                sdk,
                caller,
                kv_store=JsonKeyValueStore(home / "kv.json"),
                namespace="e9",
            )
            yield router, caller, owner, facade, sdk
        finally:
            for peer in reversed(peers):
                await peer.close()
            for client in clients:
                await client.close()
            await router.close()
            assert not router._discoveries and not router._responses
            assert not endpoint.exists()


async def claim_ready(owner, caller):
    # Drain the owner's following-status broadcast before installing new follow
    # intent. Otherwise a legitimate background restore is concurrent with the
    # explicit follow under test, and cancelling one must not cancel the other.
    received = asyncio.Event()

    def on_broadcast(envelope):
        if envelope.get("method") == "thread-stream-following-status-requested":
            received.set()

    remove = caller.client.add_broadcast_handler(on_broadcast)
    try:
        await owner.claim(THREAD, {"id": THREAD, "turns": [], "requests": []})
        await asyncio.wait_for(received.wait(), 1)
    finally:
        remove()


def message():
    return CodexStartTurnRequest(
        thread_id=THREAD, content="continue once", client_message_id="message-1"
    )


@pytest.mark.parametrize("method", ["discover_owner", "follow"])
def test_default_budget_receives_router_absence_after_silent_candidate(method):
    # Old 5 s deadlines expire before the independent 10 s router candidate timer.
    async def run():
        async with network() as (router, caller, _, _, _):
            assert await getattr(caller, method)(THREAD) is None
            assert router.last_error == "no-client-found"
            assert router.negative_responses
            assert all(
                frame["type"] == "client-discovery-response"
                and isinstance(frame["requestId"], str)
                for frame in router.negative_responses
            )
            assert not caller.client._pending

    asyncio.run(run())


def test_timeout_then_explicit_acquire_cleans_old_follow_before_resume(monkeypatch):
    # Isolate the stale-record regression from the independent default-budget bug.
    async def run():
        async with network() as (router, caller, _, facade, sdk):
            with pytest.raises(IpcError, match="follow-timeout"):
                await caller.follow(THREAD, timeout=2)
            assert caller.is_follower(THREAD)
            original_follow, original_discover = caller.follow, caller.discover_owner

            async def compatible_follow(thread_id, **kwargs):
                return await original_follow(thread_id, timeout=12, **kwargs)

            async def compatible_discover(thread_id, **kwargs):
                return await original_discover(thread_id, **{"timeout": 12, **kwargs})

            monkeypatch.setattr(caller, "follow", compatible_follow)
            monkeypatch.setattr(caller, "discover_owner", compatible_discover)
            result = await facade.start_turn(message())
            assert result.turn_id == "new-turn"
            assert caller.is_owner(THREAD) and not caller.is_follower(THREAD)
            assert [method for method, _ in sdk.calls] == [
                "thread/resume",
                "turn/start",
            ]
            assert sdk.calls[1][1]["clientUserMessageId"] == "message-1"
            assert sdk.calls[1][1]["threadId"] == THREAD
            for key, value in SETTINGS.items():
                assert sdk.calls[1][1][key] == value
            assert facade.journal.operation(THREAD)["stage"] == "confirmed"
            assert router.discovery_count == 3

    asyncio.run(run())


def test_explicit_short_deadline_is_ambiguous_and_late_response_cannot_claim():
    async def run():
        async with network() as (router, caller, _, facade, sdk):
            with pytest.raises(IpcError, match="^timeout$"):
                await caller.discover_owner(THREAD, timeout=2)
            assert not caller.client._pending
            assert not sdk.calls and not caller.is_owner(THREAD)
            assert await caller.discover_owner(THREAD, timeout=12) is None
            assert router.discovery_count == 2
            assert facade.journal.operation(THREAD) is None
            assert not sdk.calls

    asyncio.run(run())


def test_responsive_owner_wins_over_silent_candidate_without_native_fallback():
    async def run():
        async with network() as (_, caller, owner, facade, sdk):
            calls = []

            async def handle(method, params):
                calls.append((method, params))
                return {"result": {"turn": {"id": "owner-turn"}}}

            owner.owner_handler = handle
            await claim_ready(owner, caller)
            async with asyncio.timeout(5 * SCALE):
                result = await facade.start_turn(message())
            assert result.turn_id == "owner-turn"
            assert caller.get_owner(THREAD).client_id == owner.client.client_id
            assert owner.is_owner(THREAD) and not caller.is_owner(THREAD)
            assert len(calls) == 1 and calls[0][0] == "thread-follower-start-turn"
            assert (
                calls[0][1]["turnStart"]["request"]["clientUserMessageId"]
                == "message-1"
            )
            assert not sdk.calls

    asyncio.run(run())


def test_owner_appearing_during_awaited_cleanup_is_caught_by_final_guard(monkeypatch):
    async def run():
        async with network() as (router, caller, owner, facade, sdk):
            with pytest.raises(IpcError, match="follow-timeout"):
                await caller.follow(THREAD, timeout=2)
            cleanup_finished = asyncio.Event()
            original_unfollow = caller.unfollow
            calls = []

            async def handle(method, params):
                calls.append(method)
                return {"result": {"turn": {"id": "raced-owner-turn"}}}

            owner.owner_handler = handle

            async def cleanup(thread_id):
                await original_unfollow(thread_id)
                await owner.claim(thread_id, {"id": thread_id, "turns": []})
                cleanup_finished.set()

            async def guard(number):
                if number == 3:
                    assert cleanup_finished.is_set()

            monkeypatch.setattr(caller, "unfollow", cleanup)
            router.before_discovery = guard
            result = await facade.start_turn(message())
            assert result.turn_id == "raced-owner-turn"
            assert cleanup_finished.is_set()
            assert caller.get_owner(THREAD).client_id == owner.client.client_id
            assert calls == ["thread-follower-start-turn"]
            assert not sdk.calls

    asyncio.run(run())


@pytest.mark.parametrize("error", ["request-timeout", "client-disconnected"])
def test_final_discovery_error_never_resumes_or_retries(error):
    async def run():
        async with network() as (router, caller, _, facade, sdk):

            async def fail_second(number):
                return error if number == 2 else None

            router.before_discovery = fail_second
            with pytest.raises(IpcError, match=error):
                await facade.start_turn(message())
            assert router.discovery_count == 2
            assert not sdk.calls and not caller.is_owner(THREAD)
            assert facade.journal.operation(THREAD) is None

    asyncio.run(run())


def test_pinned_absence_and_owner_operation_absence_never_become_local_ownership():
    async def run():
        async with network(silent=False) as (_, caller, owner, facade, sdk):
            with pytest.raises(IpcError, match="no-client-found"):
                await caller.discover_owner(
                    THREAD, expected_owner_client_id=owner.client.client_id
                )
            await claim_ready(owner, caller)
            owner.client._handlers.pop("thread-follower-start-turn")
            with pytest.raises(IpcError, match="no-client-found"):
                await facade.start_turn(message())
            assert not sdk.calls
            assert owner.is_owner(THREAD) and not caller.is_owner(THREAD)

    asyncio.run(run())


def test_snapshot_timeout_and_cancellation_leave_no_pending_wire_requests(monkeypatch):
    async def run():
        async with network(silent=False) as (_, caller, owner, facade, sdk):
            await claim_ready(owner, caller)

            async def no_snapshot(*args, **kwargs):
                return None

            monkeypatch.setattr(owner, "_snapshot", no_snapshot)
            with pytest.raises(IpcError, match="follow-timeout"):
                await caller.follow(THREAD, timeout=2)
            assert caller.is_follower(THREAD) and not caller.is_owner(THREAD)
            assert not caller.client._pending and not sdk.calls
            task = asyncio.create_task(caller.follow(THREAD, timeout=12))
            await asyncio.sleep(SCALE)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not caller.client._pending
            assert not caller._followed[THREAD].snapshots
            assert not facade.journal.operation(THREAD)

    asyncio.run(run())


def test_two_absence_checks_and_native_cost_fit_scaled_outer_message_budget():
    async def run():
        async with network(native_cost=SCALE) as (router, caller, _, facade, sdk):
            async with asyncio.timeout(30 * SCALE):
                result = await facade.start_turn(message())
            assert result.turn_id == "new-turn"
            assert router.discovery_count == 2
            assert [method for method, _ in sdk.calls] == [
                "thread/resume",
                "turn/start",
            ]
            assert caller.is_owner(THREAD)

    asyncio.run(run())


def test_outer_timeout_during_native_start_is_unknown_and_not_resent():
    async def run():
        gate = asyncio.Event()
        async with network(start_gate=gate) as (_, _, _, facade, sdk):
            try:
                async with asyncio.timeout(30 * SCALE):
                    await facade.start_turn(message())
            except TimeoutError:
                assert sdk.started.is_set()
            else:
                pytest.fail("native start should still be pending")
            assert facade.journal.operation(THREAD)["stage"] == "unknown"
            gate.set()
            await asyncio.gather(*tuple(facade.operations.inflight))
            with pytest.raises(ValueError, match="unconfirmed native operation"):
                await facade.start_turn(message())
            assert [method for method, _ in sdk.calls] == [
                "thread/resume",
                "turn/start",
            ]

    asyncio.run(run())


def test_final_guard_local_timeout_is_not_absence():
    async def run():
        async with network() as (router, caller, _, facade, sdk):

            async def stall_second(number):
                if number == 2:
                    await asyncio.Event().wait()

            router.before_discovery = stall_second
            with pytest.raises(IpcError, match="^timeout$"):
                await facade.start_turn(message())
            assert router.discovery_count == 2
            assert not caller.client._pending
            assert not sdk.calls and not caller.is_owner(THREAD)
            assert facade.journal.operation(THREAD) is None

    asyncio.run(run())


def test_generic_owner_operation_default_remains_bounded_below_discovery_window():
    async def run():
        async with network() as (_, caller, owner, facade, sdk):

            async def stall(method, params):
                await asyncio.Event().wait()

            owner.owner_handler = stall
            await claim_ready(owner, caller)
            async with asyncio.timeout(7 * SCALE):
                with pytest.raises(IpcError, match="timeout"):
                    await facade.start_turn(message())
            assert caller.get_owner(THREAD).client_id == owner.client.client_id
            assert not sdk.calls and not caller.is_owner(THREAD)

    asyncio.run(run())
