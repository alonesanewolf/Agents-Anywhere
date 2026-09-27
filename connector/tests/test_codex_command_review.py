"""Task6b review regressions at actual dispatch and canonical publish boundaries."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_codex_commands import setup as command_setup
from test_codex_coordination_adapter import RuntimeNative
from test_codex_coordination_operations import Owner, async_test
from test_codex_runtime import FakeHost
from test_codex_runtime_coordination_review import real_runtime

from connector.core.json_kv import JsonKeyValueStore
from connector.runtime_protocol import RuntimeConfig
from connector.runtimes.codex.coordination.client import CoordinatedCodexClient
from connector.runtimes.codex.runtime import CodexRuntime
from connector.runtimes.codex.turns.goals import hydrate_goal
from connector.server.runtime_turn_rpc import dispatch_session_command_execute


class PublishingOwner(Owner):
    """Commit then yield, as the real peer does while broadcasting its snapshot."""

    adapter = None
    during_publish = None

    def is_follower(self, _):
        return False

    def get_owner(self, _):
        return SimpleNamespace(client_id="aa")

    def get_revision(self, _):
        return self.revision

    async def publish_state(self, thread_id, state):
        revision = await super().publish_state(thread_id, state)
        hook, self.during_publish = self.during_publish, None
        if hook:
            await hook()
        await self.adapter.refresh_state(thread_id, force=True)
        return revision


def goal(objective="old", status="active"):
    return {"threadId": "t", "objective": objective, "status": status, "createdAt": 1}


async def setup_owner(tmp_path, *, mid_turn=False):
    native = RuntimeNative()
    turns = (
        [{"id": "physical", "status": "inProgress", "items": []}] if mid_turn else []
    )
    peer = PublishingOwner(turns)
    peer.state["threadGoal"] = goal()
    adapter = CoordinatedCodexClient(
        native, peer, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
    )
    peer.adapter = adapter
    runtime = CodexRuntime(RuntimeConfig("codex", 8), FakeHost(), adapter)
    runtime._lifecycle.started = True
    runtime._notifications.coordination.read_selections = None
    adapter.handler = runtime._notifications.handle
    adapter.goal_support["t"] = True
    await runtime._session_states.update(
        "s", "t", status="running" if mid_turn else "idle"
    )
    await adapter.refresh_state("t", force=True)
    return runtime, adapter, peer, native


async def notify(adapter, latest):
    await adapter._native_event(
        {
            "method": "thread/goal/cleared"
            if latest is None
            else "thread/goal/updated",
            "params": {"threadId": "t", "goal": latest},
        },
        1,
    )


@pytest.mark.parametrize(
    "latest",
    [None, goal("replacement"), goal(status="complete")],
    ids=["clear", "replace", "complete"],
)
@pytest.mark.parametrize("boundary", ["native", "publish", "interrupt"])
@async_test
async def test_stop_keeps_newer_goal_and_reports_what_stopped(
    tmp_path, latest, boundary
):
    runtime, adapter, peer, native = await setup_owner(
        tmp_path, mid_turn=boundary == "interrupt"
    )

    async def later():
        await notify(adapter, latest)

    async def pause(_):
        if boundary == "native":
            await later()
        return {"goal": goal(status="paused")}

    native.responses["thread/goal/set"] = pause
    if boundary == "publish":
        peer.during_publish = later
    if boundary == "interrupt":

        async def interrupt(_):
            await later()
            return {}

        native.responses["turn/interrupt"] = interrupt
    result = await runtime.interrupt_session("s")
    assert peer.state["threadGoal"] == latest
    assert (
        runtime._session_states.get("s").metadata["codexPresentation"]["threadGoal"]
        == latest
    )
    assert result.result["goalPaused"] is False
    assert result.result["goalStopped"] is (
        latest is None or latest["status"] == "complete"
    )
    assert result.ok is (latest is None or latest["status"] == "complete")
    assert [m for m, _ in native.calls].count("thread/goal/set") == 1
    if boundary == "interrupt":
        assert result.result["interruptedTurnId"] == "physical"


@pytest.mark.parametrize("action", ["hydrate", "set", "clear"])
@pytest.mark.parametrize(
    "latest",
    [None, goal("new", "paused"), goal("done", "complete")],
    ids=["clear", "replace", "complete"],
)
@async_test
async def test_goal_publish_race_keeps_canonical_and_aa_aligned(
    tmp_path, action, latest
):
    runtime, adapter, peer, native = await setup_owner(tmp_path)

    async def later():
        await notify(adapter, latest)

    peer.during_publish = later
    native.responses.update(
        {
            "thread/goal/get": {"goal": goal()},
            "thread/goal/set": {"goal": goal("edited")},
            "thread/goal/clear": {"cleared": True},
        }
    )
    if action == "hydrate":
        await hydrate_goal(adapter, runtime._session_states, "s", "t")
    else:
        result = await runtime.execute_command(
            "s", "goal", "t", args=("edit edited" if action == "set" else "clear",)
        )
        assert result.ok, result
    assert peer.state["threadGoal"] == latest
    display = runtime._session_states.get("s").metadata["codexPresentation"]
    assert display["threadGoal"] == latest
    if latest and latest["status"] == "complete":
        assert display["completedThreadGoal"] == latest


@pytest.mark.parametrize("args", [[], ["create should not run"]])
@async_test
async def test_codex_dispatcher_preserves_explicit_empty_raw(args):
    runtime, native = await command_setup()
    result = await dispatch_session_command_execute(
        runtime,
        {
            "sessionId": "s",
            "externalSessionId": "t",
            "command": "compact" if not args else "goal",
            "raw": "",
            "args": args,
        },
    )
    assert not result["ok"] and result["code"] == "invalid_command"
    assert native.calls == []


@async_test
async def test_plan_catalog_rejects_missing_model_before_dispatch():
    runtime, native = await command_setup()
    await runtime._session_states.update(
        "s", "t", status="idle", metadata={"codexSettings": {}}
    )
    plan = next(c for c in await runtime.list_commands("s", "t") if c.id == "plan")
    assert not plan.enabled and plan.disabled_reason == "native_model_unknown"
    assert native.calls == []


@async_test
async def test_catalog_revision_ignores_stream_revision_but_keeps_real_inputs():
    runtime, _ = await command_setup()

    async def revision():
        caps = await runtime.get_session_capabilities("s", "t")
        return next(
            c for c in caps.capabilities if c.capability_id == "session.commands"
        ).metadata["catalogRevision"]

    await runtime._session_states.update(
        "s",
        "t",
        status="idle",
        metadata={
            "codexCoordination": {
                "role": "owner",
                "ownerClientId": "aa",
                "generation": [1, 1],
                "available": True,
                "revision": 1,
            }
        },
    )
    before = await revision()
    await runtime._session_states.update(
        "s",
        "t",
        status="idle",
        metadata={
            "codexCoordination": {
                "role": "owner",
                "ownerClientId": "aa",
                "generation": [1, 1],
                "available": True,
                "revision": 2,
            },
            "codexLatestTurn": {"id": "t", "status": "completed"},
            "unrelated": "stream-delta",
        },
    )
    assert await revision() == before
    for metadata in (
        {
            "codexCoordination": {
                "role": "owner",
                "ownerClientId": "other",
                "generation": [1, 1],
                "available": True,
            }
        },
        {"codexSettings": {"latestModel": "changed"}},
        {"codexPresentation": {"threadGoal": goal()}},
    ):
        await runtime._session_states.update("s", "t", status="idle", metadata=metadata)
        after = await revision()
        assert after != before
        before = after
    await runtime.stop()
    assert await revision() != before


@pytest.mark.parametrize(
    "raw,args",
    [
        ("", []),
        ("", ["do not execute"]),
        ("/feedback  first\nsecond  ", ["ignored"]),
        (None, ["fallback  text"]),
    ],
)
@async_test
async def test_dsh_dispatcher_keeps_raw_at_native_validation_boundary(raw, args):
    from connector.runtimes.dsh.runtime import DshRuntime

    calls, mutations = [], []

    async def request(method, params):
        if method == "session.getCapabilities":
            return {
                "runtime": "dsh",
                "revision": 1,
                "capabilities": [
                    {
                        "capabilityId": "session.commands",
                        "supported": True,
                        "available": True,
                        "allowed": True,
                        "metadata": {"catalogRevision": "test"},
                    }
                ],
            }
        assert method == "session.executeCommand"
        calls.append(deepcopy(params))
        line = (
            params["raw"]
            if "raw" in params
            else "/feedback" + (" " + args[0] if args else "")
        )
        if not line:
            return {
                "command": "feedback",
                "ok": False,
                "code": "invalid_command",
                "result": {},
            }
        mutations.append(line)
        return {
            "command": "feedback",
            "ok": True,
            "result": {
                "kind": "success",
                "commandId": "native",
                "executionState": "accepted",
            },
        }

    runtime = DshRuntime(RuntimeConfig("dsh", 2), SimpleNamespace(connector_id="test"))
    runtime._client = SimpleNamespace(connected=True, request=request)
    params = {
        "sessionId": "s",
        "externalSessionId": "t",
        "command": "feedback",
        "args": args,
    }
    if raw is not None:
        params["raw"] = raw
    result = await dispatch_session_command_execute(runtime, params)
    if raw == "":
        assert not result["ok"] and mutations == []
    else:
        assert result["ok"] and len(mutations) == 1
    if raw is not None:
        assert calls[0]["raw"] == raw
    else:
        assert "raw" not in calls[0]


@async_test
async def test_codex_dispatcher_omitted_raw_and_multiline_compatibility():
    runtime, native = await command_setup()
    result = await dispatch_session_command_execute(
        runtime,
        {"sessionId": "s", "externalSessionId": "t", "command": "compact", "args": []},
    )
    assert result["ok"]
    result = await dispatch_session_command_execute(
        runtime,
        {
            "sessionId": "s",
            "externalSessionId": "t",
            "command": "goal",
            "raw": "/goal create first\n second",
            "args": ["ignored"],
        },
    )
    assert result["ok"]
    assert native.calls[-1][1]["objective"] == "first\n second"


@pytest.mark.parametrize("raw", [False, 1, [], {}])
@async_test
async def test_non_string_raw_rejected_before_runtime(raw):
    from connector.runtime_protocol import RuntimeInvalidRequestError

    runtime, native = await command_setup()
    with pytest.raises(RuntimeInvalidRequestError, match="raw"):
        await dispatch_session_command_execute(
            runtime,
            {
                "sessionId": "s",
                "externalSessionId": "t",
                "command": "compact",
                "raw": raw,
            },
        )
    assert native.calls == []


@pytest.mark.parametrize("action", ["hydrate", "set", "clear", "stop"])
@pytest.mark.parametrize(
    "latest",
    [None, goal("replacement"), goal(status="complete")],
    ids=["clear", "replace", "complete"],
)
@async_test
async def test_production_peer_broadcast_race_does_not_repaint_goal(
    tmp_path, action, latest
):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _other, native):
        await runtime._session_states.update("s", "t", status="idle")
        await adapter.peer.claim(
            "t", {"id": "t", "turns": [], "requests": [], "threadGoal": goal()}
        )
        adapter.owned.add("t")
        adapter.goal_support["t"] = True
        await adapter.refresh_state("t", force=True)
        await adapter.peer._notifications.join()
        broadcast = adapter.peer.client.broadcast
        injected = False

        async def race(method, params, **kwargs):
            nonlocal injected
            await broadcast(method, params, **kwargs)
            if method == "thread-stream-state-changed" and not injected:
                injected = True
                # The production peer has committed the reply, then yielded to
                # its actual transport. A newer native event arrives before the
                # original publish_state call returns to the goal caller.
                await notify(adapter, latest)

        adapter.peer.client.broadcast = race
        native.responses.update(
            {
                "thread/goal/get": {"goal": goal()},
                "thread/goal/set": {"goal": goal(status="paused")},
                "thread/goal/clear": {"cleared": True},
            }
        )
        if action == "hydrate":
            await hydrate_goal(adapter, runtime._session_states, "s", "t")
        elif action == "stop":
            result = await runtime.interrupt_session("s")
            assert result.result["goalPaused"] is False
            assert result.ok is (latest is None or latest["status"] == "complete")
        else:
            result = await runtime.execute_command(
                "s", "goal", "t", args=("pause" if action == "set" else "clear",)
            )
            assert result.ok, result
        assert injected
        assert adapter.peer.get_state("t")["threadGoal"] == latest
        assert (
            runtime._session_states.get("s").metadata["codexPresentation"]["threadGoal"]
            == latest
        )
        await adapter.peer._notifications.join()
        assert (
            runtime._session_states.get("s").metadata["codexPresentation"]["threadGoal"]
            == latest
        )
