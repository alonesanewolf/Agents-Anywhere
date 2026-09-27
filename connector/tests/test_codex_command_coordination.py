"""Actual facade/owner boundary for goal observation and user stop."""

from test_codex_coordination_adapter import RuntimeNative
from test_codex_coordination_operations import Owner, async_test

from connector.core.json_kv import JsonKeyValueStore
from connector.runtimes.codex.coordination.client import CoordinatedCodexClient


@async_test
async def test_getter_hydrates_canonical_goal_without_notification(tmp_path):
    native, peer = RuntimeNative(), Owner()
    client = CoordinatedCodexClient(
        native, peer, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
    )
    goal = {
        "threadId": "t",
        "objective": "finish",
        "status": "complete",
        "tokenBudget": 321,
        "future": 7,
    }
    native.responses["thread/goal/get"] = {"goal": goal}
    await client.native_request("thread/goal/get", {"threadId": "t"})
    assert peer.state["threadGoal"] == goal
    assert peer.state["completedThreadGoal"] == goal
    native.responses["thread/goal/get"] = {"goal": None}
    await client.native_request("thread/goal/get", {"threadId": "t"})
    assert peer.state["threadGoal"] is None
    assert peer.state["completedThreadGoal"] == goal


@async_test
async def test_user_stop_pauses_goal_between_turns_without_expected_id(tmp_path):
    native, peer = RuntimeNative(), Owner()
    peer.state["threadGoal"] = {
        "threadId": "t",
        "objective": "finish",
        "status": "active",
    }
    client = CoordinatedCodexClient(
        native, peer, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
    )
    native.responses["thread/goal/set"] = {
        "goal": {**peer.state["threadGoal"], "status": "paused"}
    }
    result = await client.stop_session("t")
    assert result["ok"] is True
    assert native.calls == [("thread/goal/set", {"threadId": "t", "status": "paused"})]
    assert peer.state["threadGoal"]["status"] == "paused"


@async_test
async def test_getter_does_not_replace_newer_live_goal(tmp_path):
    native, peer = RuntimeNative(), Owner()
    client = CoordinatedCodexClient(
        native, peer, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
    )
    old = {"threadId": "t", "objective": "old", "status": "active"}
    new = {"threadId": "t", "objective": "new", "status": "paused"}

    async def get(_):
        await client.observe_goal("t", new)
        return {"goal": old}

    native.responses["thread/goal/get"] = get
    result = await client.native_request("thread/goal/get", {"threadId": "t"})
    assert result["goal"] == new
    assert peer.state["threadGoal"] == new


@async_test
async def test_control_dispatch_never_claims_after_owner_loss(tmp_path):
    from connector.runtimes.codex.turns.coordination_controls import (
        execute_coordination_control,
    )

    native, peer = RuntimeNative(), Owner()
    peer.owned = False
    peer.is_follower = lambda _: False
    client = CoordinatedCodexClient(
        native, peer, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
    )
    import pytest

    with pytest.raises(ValueError, match="existing owner"):
        await execute_coordination_control(client, "t", "compact-thread", {})
    assert native.calls == []


@async_test
async def test_user_stop_mid_turn_pauses_but_machine_interrupt_does_not(tmp_path):
    from connector.runtimes.codex.sdk.runtime_client import CodexInterruptTurnRequest

    native, peer = (
        RuntimeNative(),
        Owner([{"id": "physical", "status": "inProgress", "items": []}]),
    )
    goal = {"threadId": "t", "objective": "finish", "status": "active"}
    peer.state["threadGoal"] = goal
    client = CoordinatedCodexClient(
        native, peer, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
    )
    native.responses["thread/goal/set"] = {"goal": {**goal, "status": "paused"}}
    await client.interrupt_turn(
        CodexInterruptTurnRequest(thread_id="t", turn_id="physical")
    )
    assert [method for method, _ in native.calls] == ["turn/interrupt"]
    native.calls.clear()
    result = await client.stop_session("t")
    assert result["goalPaused"] is True
    assert [method for method, _ in native.calls] == [
        "thread/goal/set",
        "turn/interrupt",
    ]


@async_test
async def test_native_unknown_pause_does_not_claim_success(tmp_path):
    native, peer = RuntimeNative(), Owner()
    peer.state["threadGoal"] = {
        "threadId": "t",
        "objective": "finish",
        "status": "active",
    }
    client = CoordinatedCodexClient(
        native, peer, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
    )
    native.responses["thread/goal/set"] = TimeoutError("secret")
    result = await client.stop_session("t")
    assert result["goalPaused"] is False and result["goalPauseError"]
    assert "secret" not in str(result)
    assert peer.state["threadGoal"]["status"] == "active"


@async_test
async def test_owned_goal_getter_reaches_real_peer_follower(tmp_path):
    import asyncio
    import tempfile
    from pathlib import Path

    from connector.runtimes.codex.coordination.peer import CoordinationPeer
    from connector.runtimes.codex.coordination.projection import native_to_state
    from connector.runtimes.codex.coordination.transport import CoordinationClient

    with tempfile.TemporaryDirectory(prefix="aa-goal-", dir="/tmp") as directory:
        endpoint = Path(directory) / "ipc.sock"
        owner = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        follower = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        native = RuntimeNative()
        adapter = CoordinatedCodexClient(
            native, owner, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
        )

        async def event(_):
            pass

        await adapter.start(event)
        await follower.start()
        try:
            await owner.claim(
                "t", native_to_state({"id": "t", "turns": []}, complete=True)
            )
            await follower.follow("t")
            goal = {
                "threadId": "t",
                "objective": "done",
                "status": "complete",
                "tokenBudget": 77,
            }
            native.responses["thread/goal/get"] = {"goal": goal}
            await adapter.native_request("thread/goal/get", {"threadId": "t"})
            async with asyncio.timeout(2):
                while follower.get_state("t").get("threadGoal") != goal:
                    await asyncio.sleep(0.01)
            assert follower.get_state("t")["completedThreadGoal"] == goal
            assert [method for method, _ in native.calls] == ["thread/goal/get"]
            assert adapter.command_capabilities("t")["goalSupported"]
        finally:
            await follower.close()
            await adapter.stop()


@async_test
async def test_unrelated_event_during_goal_get_does_not_invent_null(tmp_path):
    native, peer = RuntimeNative(), Owner()
    client = CoordinatedCodexClient(
        native, peer, kv_store=JsonKeyValueStore(tmp_path / "kv"), namespace="test"
    )
    goal = {"threadId": "t", "objective": "work", "status": "active"}

    async def get(_):
        await client._native_event(
            {
                "method": "thread/name/updated",
                "params": {"threadId": "t", "threadName": "changed"},
            },
            1,
        )
        return {"goal": goal}

    native.responses["thread/goal/get"] = get
    result = await client.native_request("thread/goal/get", {"threadId": "t"})
    assert result["goal"] == goal
    assert peer.state["threadGoal"] == goal
