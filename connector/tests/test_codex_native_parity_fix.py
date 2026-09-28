"""Review regressions for physical activity and the follower send boundary."""

import pytest
from connector.runtimes.codex.coordination.projection import (
    active_turn,
    state_to_native,
)
from connector.runtimes.codex.coordination.reducer import reduce_event
from connector.runtimes.codex.domain.thread_state import thread_status
from test_codex_coordination_operations import async_test, setup
from test_codex_native_parity import event, idle, started
from test_codex_runtime_coordination_review import owner_state, real_runtime


@pytest.mark.parametrize("operation", ["start", "steer"])
@pytest.mark.parametrize("after_dispatch", [None, "idle", "newer"])
@async_test
async def test_real_follower_discovery_idle_precedes_valid_ack(
    tmp_path, operation, after_dispatch
):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, owner, _native):
        state = {**owner_state(), "threadRuntimeStatus": {"type": "idle"}}
        if operation == "steer":
            state["threadRuntimeStatus"] = {"type": "active"}
            state["turns"] = [
                {"turnId": "original", "status": "inProgress", "items": []}
            ]
        await owner.claim("remote", state)
        await runtime.prepare_session_view("view", "remote")
        calls = []

        async def consume(method, params):
            calls.append((method, params))
            if after_dispatch:
                current = reduce_event(owner.get_state("remote"), idle())
                if after_dispatch == "newer":
                    current = reduce_event(current, started("newer"))
                revision = await owner.publish_state("remote", current)
                await adapter.peer.wait_revision("remote", revision, timeout=2)
            return (
                {
                    "result": {
                        "turn": {"id": "accepted", "status": "inProgress", "items": []}
                    }
                }
                if operation == "start"
                else {"result": {"turnId": "accepted"}}
            )

        owner.owner_handler = consume
        request = adapter.peer.request_owner
        discover = adapter.peer.discover_owner
        armed = False

        async def request_owner(*args, **kwargs):
            nonlocal armed
            armed = True
            return await request(*args, **kwargs)

        async def discovery(*args, **kwargs):
            result = await discover(*args, **kwargs)
            if armed:
                assert calls == []
                revision = await owner.publish_patches(
                    "remote",
                    [
                        {
                            "op": "replace",
                            "path": ["threadRuntimeStatus"],
                            "value": {"type": "idle"},
                        }
                    ],
                )
                await adapter.peer.wait_revision("remote", revision, timeout=2)
            return result

        adapter.peer.request_owner = request_owner
        adapter.peer.discover_owner = discovery
        result = await (
            runtime.start_turn("view", "remote", "fresh")
            if operation == "start"
            else runtime.steer_turn("view", "remote", "fresh")
        )
        assert result.ok and result.result["turnId"] == "accepted"
        assert len(calls) == 1
        assert runtime._active_turn_ids.get("view") == (
            "accepted"
            if after_dispatch is None
            else "newer"
            if after_dispatch == "newer"
            else None
        )
        assert runtime._session_states.get("view").status == (
            "idle" if after_dispatch == "idle" else "running"
        )


@pytest.mark.parametrize("operation", ["start", "steer"])
@pytest.mark.parametrize("terminal", ["failed", "interrupted", "cancelled"])
@async_test
async def test_terminal_history_correction_preserves_new_and_retained_ack(
    tmp_path, operation, terminal
):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, owner, _native):
        state = {
            **owner_state(),
            "threadRuntimeStatus": {"type": "idle"},
            "turns": [{"turnId": "history", "status": "completed", "items": []}],
        }
        if operation == "steer":
            state["threadRuntimeStatus"] = {"type": "active"}
            state["turns"].append(
                {"turnId": "original", "status": "inProgress", "items": []}
            )
        await owner.claim("remote", state)
        await runtime.prepare_session_view("view", "remote")

        async def correction(status):
            revision = await owner.publish_patches(
                "remote",
                [{"op": "replace", "path": ["turns", 0, "status"], "value": status}],
            )
            await adapter.peer.wait_revision("remote", revision, timeout=2)

        async def consume(method, params):
            await correction(terminal)
            return (
                {
                    "result": {
                        "turn": {"id": "accepted", "status": "inProgress", "items": []}
                    }
                }
                if operation == "start"
                else {"result": {"turnId": "accepted"}}
            )

        owner.owner_handler = consume
        await (
            runtime.start_turn("view", "remote", "fresh")
            if operation == "start"
            else runtime.steer_turn("view", "remote", "fresh")
        )
        assert runtime._active_turn_ids.get("view") == "accepted"
        await correction("completed")
        await adapter.refresh_state("remote", force=True)
        assert runtime._active_turn_ids.get("view") == "accepted"
        assert runtime._session_states.get("view").status == "running"


def status_only_active():
    state = reduce_event(owner_state(), started("residual"))
    state = reduce_event(state, idle())
    return reduce_event(
        state, event("thread/status/changed", status={"type": "active"})
    )


def test_unrelated_completion_keeps_status_only_active_and_goal():
    state = status_only_active()
    state = reduce_event(
        state,
        event(
            "turn/completed",
            turn={"id": "residual", "status": "completed", "items": []},
        ),
    )
    assert thread_status(state_to_native(state)) == "running"
    assert active_turn(state) is None
    assert state["threadRuntimeStatus"] == {"type": "active"}
    assert state["turns"][0]["status"] == "completed"
    assert state["threadGoal"] == {"objective": "keep goal"}


@async_test
async def test_status_only_active_blocks_start_and_queue_without_guessing_control_target(
    tmp_path,
):
    from connector.runtimes.codex.coordination.queue import execute_head

    operations, native, peer, journal = setup(tmp_path)
    state = status_only_active()
    state["id"] = "t"
    state = reduce_event(
        state,
        event(
            "turn/completed",
            turn={
                "id": "residual",
                "status": "completed",
                "items": [
                    {"id": "old-final", "type": "agentMessage", "text": "old final"}
                ],
            },
        ),
    )
    peer.state = state
    with pytest.raises(ValueError, match="active"):
        await operations.handle(
            "thread-follower-start-turn",
            {
                "conversationId": "t",
                "turnStart": {
                    "request": {
                        "threadId": "t",
                        "input": [{"type": "text", "text": "no"}],
                    },
                    "context": {},
                },
            },
        )
    await journal.replace_queue("t", [{"id": "queued", "text": "later"}])
    assert await execute_head(operations, "t") is False
    with pytest.raises(ValueError, match="target"):
        await operations.handle(
            "thread-follower-interrupt-turn",
            {"conversationId": "t", "mode": "user-stop"},
        )
    with pytest.raises(ValueError, match="active"):
        await operations.handle(
            "thread-follower-steer-turn",
            {"conversationId": "t", "input": [{"type": "text", "text": "no"}]},
        )
    assert native.calls == []
    assert journal.queue("t")[0]["id"] == "queued"


@async_test
async def test_public_no_target_steer_does_not_repaint_native_running(tmp_path):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, owner, _native):
        state = reduce_event(
            status_only_active(),
            event("turn/completed", turn={"id": "residual", "status": "completed"}),
        )
        await owner.claim("remote", state)
        await runtime.prepare_session_view("view", "remote")
        assert runtime._session_states.get("view").status == "running"
        result = await runtime.steer_turn("view", "remote", "keep draft")
        assert not result.ok
        assert runtime._session_states.get("view").status == "running"
        assert "view" not in runtime._active_turn_ids
        assert thread_status((await adapter.read_thread("remote")).thread) == "running"


@async_test
async def test_current_completion_after_item_before_start_ack_stays_terminal(tmp_path):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        native.configure_resume("remote")

        async def start(_):
            await native.handler(
                event(
                    "item/completed",
                    turnId="accepted",
                    item={"id": "out", "type": "agentMessage", "text": "done"},
                ),
                1,
            )
            await native.handler(
                event("turn/completed", turn={"id": "accepted", "status": "completed"}),
                1,
            )
            return {"turn": {"id": "accepted", "status": "inProgress", "items": []}}

        native.responses["turn/start"] = start
        result = await runtime.start_turn("view", "remote", "fresh")
        assert result.ok
        assert runtime._session_states.get("view").status == "idle"
        assert "view" not in runtime._active_turn_ids
        assert adapter.peer.get_state("remote")["turns"][0]["status"] == "completed"


@async_test
async def test_public_rejected_start_preserves_known_targetless_native_activity(
    tmp_path,
):
    async with real_runtime(tmp_path) as (runtime, _host, _adapter, _owner, native):
        native.configure_resume("remote")
        native.responses["turn/start"] = {
            "turn": {"id": "residual", "status": "inProgress", "items": []}
        }
        await runtime.start_turn("view", "remote", "first")
        await native.handler(idle(), 1)
        await native.handler(
            event("thread/status/changed", status={"type": "active"}), 1
        )
        await native.handler(
            event("turn/completed", turn={"id": "residual", "status": "completed"}), 1
        )
        with pytest.raises(ValueError, match="active"):
            await runtime.start_turn("view", "remote", "not dispatched")
        assert (
            len([method for method, _ in native.calls if method == "turn/start"]) == 1
        )
        assert runtime._session_states.get("view").status == "running"
        assert "view" not in runtime._active_turn_ids
