"""Real runtime/handlers/preflight over private sockets; only native I/O is fake."""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

# The separately managed connector has its own official SDK dependency. Reuse
# the installed test environment without replacing server dependencies.
CONNECTOR = Path(__file__).resolve().parents[2] / "connector"
sys.path.extend([str(CONNECTOR), str(CONNECTOR / "tests")])
sys.path.extend(
    str(path) for path in (CONNECTOR / ".venv/lib").glob("python*/site-packages")
)

from agent_server.core.models import MessageCreateRequest, SessionView
from agent_server.services.session_run import (
    SessionRunService,
    SessionRunTimeoutError,
)
from connector.runtime_protocol import RuntimeConfig
from connector.runtimes.codex.runtime import CodexRuntime
from connector.server.runtime_session_rpc import (
    read_session_capabilities,
    read_session_notices,
    read_session_state,
)
from connector.server.runtime_turn_rpc import (
    dispatch_session_send_message,
)
from test_codex_owner_absence_budget import (
    SCALE,
    THREAD,
    ScaledDeadlines,
    network,
    peer_module,
    router_module,
    transport_module,
)
from test_codex_runtime import FakeCodexClient, FakeHost


class Native(FakeCodexClient):
    native_generation = 1

    def __init__(self):
        super().__init__()
        self.calls = []
        self.reading = asyncio.Event()
        self.read_gate = None
        self.settings = {
            "model": "gpt-6-luna",
            "effort": "low",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandboxPolicy": {"type": "workspaceWrite"},
        }

    def set_native_event_handler(self, handler):
        self.native_handler = handler

    def native_runtime_info(self):
        return {"version": "0.155.1"}

    async def native_thread_resume(self, thread_id):
        self.calls.append(("thread/resume", {"threadId": thread_id}))
        return self.response(thread_id)

    def response(self, thread_id):
        return {
            "thread": {"id": thread_id, "turns": [], "status": {"type": "idle"}},
            **self.settings,
        }

    async def native_request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            self.reading.set()
            if self.read_gate:
                await self.read_gate.wait()
            await asyncio.sleep(0.01)
            return self.response(params["threadId"])
        if method == "turn/start":
            return {"turn": {"id": "new-turn", "status": "inProgress", "items": []}}
        if method == "thread/goal/get":
            return {"goal": None}
        raise AssertionError(method)


@pytest.fixture(autouse=True)
def deadlines(monkeypatch):
    monkeypatch.delenv("AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS", raising=False)
    for module in (peer_module, router_module, transport_module):
        monkeypatch.setattr(module, "asyncio", ScaledDeadlines())
    from connector.runtimes.codex.sessions import observers

    if hasattr(observers, "PREPARE_TIMEOUT_SECONDS"):
        monkeypatch.setattr(observers, "PREPARE_TIMEOUT_SECONDS", 17 * SCALE)


@asynccontextmanager
async def runtime_network():
    async with network() as (router, caller, owner, facade, _):
        native, host = Native(), FakeHost()
        facade.sdk = native
        facade.operations.sdk = native
        runtime = CodexRuntime(
            RuntimeConfig(runtime="codex", revision=1, values={"appIntegration": True}),
            host,
            facade,
        )
        await runtime.start()
        try:
            yield SimpleNamespace(
                runtime=runtime,
                host=host,
                native=native,
                router=router,
                caller=caller,
                owner=owner,
                facade=facade,
            )
        finally:
            await runtime.stop()


PARAMS = {
    "sessionId": "session",
    "externalSessionId": THREAD,
    "runtime": "codex",
    "runtimeId": "codex",
}


class RpcBoundary:
    def __init__(self, context):
        self.context, self.calls, self.tasks = context, [], []

    async def is_online(self, _):
        return True

    async def request(self, _, method, params, timeout=30):
        self.calls.append((method, timeout))
        c = self.context

        async def dispatch():
            if method == "session.state":
                result = await read_session_state(c.runtime, c.host, params)
                result["state"]["runtimeId"] = "codex"
                return result
            if method == "session.capabilities":
                result = await read_session_capabilities(c.runtime, params)
                result["capabilitySet"]["runtimeId"] = "codex"
                return result
            if method == "session.send_message":
                return await dispatch_session_send_message(c.runtime, params)
            raise AssertionError(method)

        async def wire_dispatch():
            from agent_server.infra.connector_rpc import ConnectorRpcError
            from connector.runtime_protocol import RuntimeProtocolError

            try:
                return await dispatch()
            except RuntimeProtocolError as exc:
                raise ConnectorRpcError(exc.code, str(exc)) from exc

        task = asyncio.create_task(wire_dispatch())
        self.tasks.append(task)
        # Actual server timeout does not cancel the remote Connector request.
        return await asyncio.wait_for(asyncio.shield(task), timeout * SCALE)

    async def drain(self):
        await asyncio.gather(*self.tasks, return_exceptions=True)


class Store:
    def __init__(self):
        self.started = []
        self.session = SessionView(
            id="session",
            connectorId="connector",
            connectorStatus="online",
            runtime="codex",
            externalSessionId=THREAD,
            status="idle",
            takeover=True,
            updatedSeq=1,
        )

    async def get_session(self, *_args, **_kwargs):
        return self.session

    async def start_active_run(self, **kwargs):
        self.started.append(kwargs)

    async def clear_active_run(self, _):
        pass


class Devices:
    async def ensure_session_routable(self, *_args, **_kwargs):
        pass


def test_public_state_and_notices_coalesce_one_owner_discovery():
    async def run():
        async with runtime_network() as c:
            state, _ = await asyncio.gather(
                read_session_state(c.runtime, c.host, PARAMS),
                read_session_notices(c.runtime, PARAMS),
            )
            assert state["state"]["status"] == "idle"
            assert c.router.discovery_count == 1
            assert not any(
                method in {"thread/resume", "turn/start"}
                for method, _ in c.native.calls
            )
            await read_session_state(c.runtime, c.host, PARAMS)
            assert c.router.discovery_count == 2  # Fresh read; no negative cache.

    asyncio.run(run())


def test_real_send_preflight_can_finish_before_caller_budget():
    async def run():
        async with runtime_network() as c:
            rpc, store = RpcBoundary(c), Store()
            try:
                result = await SessionRunService(store, rpc, Devices()).send_message(
                    "session",
                    MessageCreateRequest(content="once", clientMessageId="e13-message"),
                    user_id="user",
                )
                assert result.ok
                assert len(store.started) == 1
                assert rpc.calls[0] == ("session.state", 20)
                assert rpc.calls[-1] == ("session.send_message", 30)
                assert (
                    c.router.discovery_count == 3
                )  # Passive read + BOTH fresh acquire guards.
                starts = [p for method, p in c.native.calls if method == "turn/start"]
                assert len(starts) == 1
                assert starts[0]["threadId"] == THREAD
                assert starts[0]["sandboxPolicy"] == {"type": "workspaceWrite"}
                assert starts[0]["model"] == "gpt-6-luna"
                assert starts[0]["effort"] == "low"
                assert starts[0]["approvalPolicy"] == "on-request"
            finally:
                await rpc.drain()

    asyncio.run(run())


def test_explicit_short_preflight_timeout_is_structured_and_does_not_send(monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS", "1")

    async def run():
        async with runtime_network() as c:
            rpc, store = RpcBoundary(c), Store()
            try:
                with pytest.raises(SessionRunTimeoutError) as error:
                    await SessionRunService(store, rpc, Devices()).send_message(
                        "session",
                        MessageCreateRequest(content="no send"),
                        user_id="user",
                    )
                assert error.value.detail["code"] == "runtime_state_timeout"
                assert not store.started
                assert [method for method, _ in rpc.calls] == ["session.state"]
            finally:
                await rpc.drain()
            assert (
                await c.runtime.get_session_state("session", THREAD)
            ).status == "idle"
            assert not any(
                method in {"thread/resume", "turn/start"}
                for method, _ in c.native.calls
            )

    asyncio.run(run())


def test_slow_ownerless_view_does_not_block_owned_view_or_other_waiter():
    async def run():
        async with runtime_network() as c:
            c.native.read_gate = asyncio.Event()
            first = asyncio.create_task(read_session_state(c.runtime, c.host, PARAMS))
            second = asyncio.create_task(read_session_notices(c.runtime, PARAMS))
            await c.native.reading.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            await c.caller.claim("fast", {"id": "fast", "turns": [], "requests": []})
            fast = await asyncio.wait_for(
                read_session_state(
                    c.runtime,
                    c.host,
                    {
                        **PARAMS,
                        "sessionId": "fast-session",
                        "externalSessionId": "fast",
                    },
                ),
                0.1,
            )
            assert fast["state"]["metadata"]["codexCoordination"]["role"] == "owner"
            c.native.read_gate.set()
            await second
            assert c.router.discovery_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("change", ["owner", "native", "reset"])
def test_passive_native_read_cannot_publish_after_authority_changes(change):
    async def run():
        async with runtime_network() as c:
            c.native.read_gate = asyncio.Event()
            task = asyncio.create_task(read_session_state(c.runtime, c.host, PARAMS))
            await c.native.reading.wait()
            if change == "owner":
                observed = asyncio.Event()
                remove = c.caller.client.add_broadcast_handler(
                    lambda e: (
                        observed.set()
                        if e.get("method") == "thread-stream-following-status-requested"
                        else None
                    )
                )
                try:
                    await c.owner.claim(
                        THREAD, {"id": THREAD, "turns": [], "requests": []}
                    )
                    await observed.wait()
                finally:
                    remove()
            elif change == "native":
                c.native.native_generation += 1
            else:
                c.facade._view_signal(
                    {
                        "method": "ipc-connection-reset",
                        "version": 1,
                        "sourceClientId": c.caller.client.client_id,
                        "params": {},
                    }
                )
            c.native.read_gate.set()
            with pytest.raises(Exception) as error:
                await task
            assert error.value.code == "codex_view_changed"
            assert not [
                s
                for s in c.host.state_updates
                if s["metadata"].get("source") == "codex.thread/read.state"
            ]
            assert not any(m == "thread/resume" for m, _ in c.native.calls)

    asyncio.run(run())


def test_catalog_wait_rechecks_authority_and_ignores_unrelated_signals(monkeypatch):
    async def run():
        async with runtime_network() as c:
            entered, release = asyncio.Event(), asyncio.Event()
            original = c.runtime._session_reader.selections_from_thread

            async def selections(_reader, thread):
                entered.set()
                await release.wait()
                return await original(thread)

            monkeypatch.setattr(
                type(c.runtime._session_reader), "selections_from_thread", selections
            )
            task = asyncio.create_task(read_session_state(c.runtime, c.host, PARAMS))
            await entered.wait()
            signal = {
                "method": "thread-stream-following-status-requested",
                "version": 1,
                "sourceClientId": "other",
                "params": {"hostId": c.caller.host_id, "conversationId": "unrelated"},
            }
            c.facade._view_signal(signal)
            c.facade._view_signal(
                {**signal, "params": {"hostId": "wrong-host", "conversationId": THREAD}}
            )
            c.facade._view_signal(
                {
                    **signal,
                    "version": 999,
                    "params": {"hostId": c.caller.host_id, "conversationId": THREAD},
                }
            )
            token = c.facade.view_token(THREAD)
            c.facade._view_signal(
                {
                    "method": "client-status-changed",
                    "version": 0,
                    "sourceClientId": "other",
                    "params": {"clientId": "other", "status": "connected"},
                }
            )
            assert c.facade.view_is_current(THREAD, token)
            release.set()
            assert (await task)["state"]["status"] == "idle"
            # A generation change during selection normalization is fenced too.
            entered.clear()
            release.clear()
            task = asyncio.create_task(read_session_state(c.runtime, c.host, PARAMS))
            await entered.wait()
            before = len(c.host.state_updates)
            c.native.native_generation += 1
            release.set()
            with pytest.raises(Exception) as error:
                await task
            assert error.value.code == "codex_view_changed"
            assert len(c.host.state_updates) == before

    asyncio.run(run())


@pytest.mark.parametrize("protect", ["busy", "owner"])
def test_eviction_rechecks_after_victim_lock_wait(protect):
    async def run():
        async with runtime_network() as c:
            observer = c.runtime._observers
            observer.LIMIT = 1
            await read_session_state(c.runtime, c.host, PARAMS)
            async with c.facade.locks[THREAD]:
                task = asyncio.create_task(
                    c.runtime.prepare_session_view("other-session", "other-thread")
                )
                # Synchronize on reservation, before detach can acquire the lock.
                while THREAD not in observer.evicting:
                    await asyncio.sleep(0)
                if protect == "busy":
                    observer.busy["session"] += 1
                else:
                    await c.caller.claim(
                        THREAD, {"id": THREAD, "turns": [], "requests": []}
                    )
            with pytest.raises(Exception) as error:
                await task
            assert error.value.code == "codex_view_changed"
            assert THREAD in c.facade.attached
            assert list(observer.views) == [THREAD]
            assert not observer.inflight and not observer.evicting
            assert "other-thread" not in c.facade.attached

    asyncio.run(run())


def test_clear_cancels_old_prepare_without_erasing_new_entry_or_counter():
    from connector.runtimes.codex.sessions.observers import guarded_mutation

    async def run():
        async with runtime_network() as c:
            observer = c.runtime._observers
            c.native.read_gate = asyncio.Event()
            task = asyncio.create_task(
                c.runtime.prepare_session_view("session", THREAD)
            )
            await c.native.reading.wait()
            entered, finish = asyncio.Event(), asyncio.Event()

            @guarded_mutation(allow_unavailable=True)
            async def mutation(runtime, session_id):
                entered.set()
                await finish.wait()

            mutation_task = asyncio.create_task(mutation(c.runtime, "session"))
            await entered.wait()
            old_busy = observer.busy
            old_tasks = observer.clear()
            observer.busy["session"] = 2
            next_task = asyncio.create_task(
                c.runtime.prepare_session_view("session", THREAD)
            )
            await asyncio.sleep(0)
            finish.set()
            await mutation_task
            assert not old_busy
            assert observer.busy["session"] == 2
            await asyncio.gather(*old_tasks, return_exceptions=True)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert THREAD in observer.inflight
            c.native.read_gate.set()
            await next_task
            assert list(observer.views) == [THREAD]
            assert not observer.inflight

    asyncio.run(run())


def test_dispose_cancels_native_read_and_drains_preparation():
    async def run():
        async with runtime_network() as c:
            c.native.read_gate = asyncio.Event()
            task = asyncio.create_task(read_session_state(c.runtime, c.host, PARAMS))
            await c.native.reading.wait()
            await c.runtime._observers.close()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not c.runtime._observers.inflight
            assert not c.runtime._observers.views
            assert not [
                s
                for s in c.host.state_updates
                if s["metadata"].get("source") == "codex.thread/read.state"
            ]

    asyncio.run(run())


def test_read_budget_preserves_other_runtime_and_configured_values(monkeypatch):
    from agent_server.api.sessions import _session_rpc_timeout_seconds
    from agent_server.services.session_read_budget import session_read_timeout_seconds

    assert session_read_timeout_seconds("codex") == 20
    assert session_read_timeout_seconds("claude") == 10
    assert _session_rpc_timeout_seconds("codex") == 20
    for value, expected in [("0.15", 0.15), ("45", 45), ("invalid", 10)]:
        monkeypatch.setenv("AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS", value)
        assert session_read_timeout_seconds("codex") == expected
        assert _session_rpc_timeout_seconds("claude") == expected

    async def run():
        class Rpc:
            async def request(self, *args, timeout):
                assert timeout == 10  # Non-Codex preflight retains its prior bound.
                return {
                    "state": {
                        "runtime": "claude",
                        "runtimeId": "claude",
                        "status": "idle",
                    }
                }

        session = Store().session.model_copy(
            update={"runtime": "claude", "runtimeId": "claude"}
        )
        assert (
            await SessionRunService(None, Rpc(), None)._read_runtime_status(session)
            == "idle"
        )

    asyncio.run(run())


def test_shared_prepare_deadline_is_structured_and_prevents_send():
    async def run():
        async with runtime_network() as c:
            c.native.read_gate = asyncio.Event()
            rpc, store = RpcBoundary(c), Store()
            try:
                with pytest.raises(SessionRunTimeoutError) as error:
                    await SessionRunService(store, rpc, Devices()).send_message(
                        "session",
                        MessageCreateRequest(content="no send"),
                        user_id="user",
                    )
                assert error.value.detail["code"] == "runtime_state_timeout"
                assert not store.started
                assert not c.runtime._observers.inflight
                assert [m for m, _ in rpc.calls] == ["session.state"]
                assert not any(
                    m in {"thread/resume", "turn/start"} for m, _ in c.native.calls
                )
            finally:
                await rpc.drain()

    asyncio.run(run())


def test_capacity_counts_inflight_and_lru_eviction_releases_view():
    async def run():
        async with runtime_network() as c:
            observer = c.runtime._observers
            observer.LIMIT = 1
            c.native.read_gate = asyncio.Event()
            first = asyncio.create_task(
                c.runtime.prepare_session_view("session", THREAD)
            )
            await c.native.reading.wait()
            with pytest.raises(RuntimeError, match="capacity"):
                await c.runtime.prepare_session_view("other", "other")
            assert len(observer.inflight) == 1
            assert c.router.discovery_count == 1
            c.native.read_gate.set()
            await first
            await c.runtime.prepare_session_view("other", "other")
            assert list(observer.views) == ["other"]
            assert c.facade.attached == {"other"}
            assert set(c.facade.view_epochs) == {"other"}
            observer.LIMIT = 2
            await c.runtime.prepare_session_view("session", THREAD)
            await c.runtime.prepare_session_view("other", "other")
            await c.runtime.prepare_session_view("third", "third")
            assert list(observer.views) == ["other", "third"]
            assert c.facade.attached == {"other", "third"}

    asyncio.run(run())


def test_canonical_projection_checks_generation_after_catalog_await():
    async def run():
        async with runtime_network() as c:
            await c.caller.claim(THREAD, {"id": THREAD, "turns": [], "requests": []})
            await c.caller._notifications.join()
            entered, release = asyncio.Event(), asyncio.Event()
            original = c.runtime._notifications.coordination.read_selections

            async def selections(thread):
                entered.set()
                await release.wait()
                return await original(thread)

            c.runtime._notifications.coordination.read_selections = selections
            task = asyncio.create_task(
                c.runtime.prepare_session_view("session", THREAD)
            )
            await entered.wait()
            before = len(c.host.state_updates)
            c.native.native_generation += 1
            release.set()
            with pytest.raises(Exception) as error:
                await task
            assert error.value.code == "codex_view_changed"
            assert len(c.host.state_updates) == before

    asyncio.run(run())


def test_new_owner_after_passive_absence_receives_explicit_send():
    async def run():
        async with runtime_network() as c:
            received = []

            async def owner_operation(method, params):
                received.append((method, params))
                return {
                    "result": {
                        "turn": {
                            "id": "owner-turn",
                            "status": "inProgress",
                            "items": [],
                        }
                    }
                }

            c.owner.owner_handler = owner_operation
            rpc, store = RpcBoundary(c), Store()
            original = rpc.request

            async def request(connector, method, params, timeout=30):
                if method == "session.send_message":
                    await c.owner.claim(
                        THREAD, {"id": THREAD, "turns": [], "requests": []}
                    )
                return await original(connector, method, params, timeout)

            rpc.request = request
            try:
                result = await SessionRunService(store, rpc, Devices()).send_message(
                    "session",
                    MessageCreateRequest(
                        content="new owner", clientMessageId="owner-message"
                    ),
                    user_id="user",
                )
                assert result.ok
                assert len(received) == 1
                assert received[0][0] == "thread-follower-start-turn"
                assert not any(
                    m in {"thread/resume", "turn/start"} for m, _ in c.native.calls
                )
            finally:
                await rpc.drain()

    asyncio.run(run())


def test_clear_during_eviction_cannot_remove_readded_view():
    async def run():
        async with runtime_network() as c:
            observer = c.runtime._observers
            observer.LIMIT = 1
            await c.runtime.prepare_session_view("session", THREAD)
            async with c.facade.locks[THREAD]:
                old = asyncio.create_task(
                    c.runtime.prepare_session_view("replacement", "replacement")
                )
                while THREAD not in observer.evicting:
                    await asyncio.sleep(0)
                pending = observer.clear()
                new = asyncio.create_task(
                    c.runtime.prepare_session_view("session", THREAD)
                )
                await asyncio.sleep(0)
                await asyncio.gather(*pending, return_exceptions=True)
                assert THREAD in observer.inflight
                assert not observer.evicting
            with pytest.raises(asyncio.CancelledError):
                await old
            await new
            assert list(observer.views) == [THREAD]
            assert THREAD in c.facade.attached
            assert not observer.inflight
            await observer.close()
            with pytest.raises(Exception) as error:
                await observer.prepare("session", THREAD)
            assert error.value.code == "codex_view_changed"

    asyncio.run(run())


def test_repeated_failures_retain_only_bounded_unavailable_views_then_retry():
    from connector.runtimes.codex.sessions.observers import CodexViewTimeout

    async def run():
        async with runtime_network() as c:
            observer = c.runtime._observers
            observer.LIMIT = 2
            c.native.read_gate = asyncio.Event()
            for index in range(3):
                params = {
                    **PARAMS,
                    "sessionId": f"failed-{index}",
                    "externalSessionId": f"failed-{index}",
                }
                with pytest.raises(CodexViewTimeout):
                    await read_session_state(c.runtime, c.host, params)
                assert len(observer.views) <= 2
                assert len(c.facade.attached) <= 2
                assert len(c.facade.view_epochs) <= 2
                assert not observer.inflight and not observer.evicting
                state = observer.states.get(params["sessionId"])
                assert state.status == "blocked"
                assert state.metadata["codexCoordination"]["available"] is False
                # Explicit no-client-found removed the peer's temporary follow;
                # only bounded local view intent is retained after native timeout.
                assert not c.caller.is_follower(params["externalSessionId"])
            before = c.router.discovery_count
            c.native.read_gate.set()
            state = await read_session_state(c.runtime, c.host, params)
            assert state["state"]["status"] == "idle"
            assert state["state"]["metadata"]["codexCoordination"] == {
                "role": "unattached",
                "available": True,
            }
            assert c.router.discovery_count == before + 1

    asyncio.run(run())
