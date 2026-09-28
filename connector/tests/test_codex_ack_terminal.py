"""A target's canonical terminal evidence overrides its local ACK activity."""

from copy import deepcopy

import pytest
from connector.runtimes.codex.coordination.state import enumerate_turns
from connector.runtimes.codex.domain.thread_state import thread_status
from test_codex_coordination_operations import async_test
from test_codex_runtime_coordination_review import owner_state, real_runtime


def initial_state(paged):
    state = {**owner_state(), "threadRuntimeStatus": {"type": "idle"}}
    if paged:
        state["turnHistory"] = {
            "kind": "canonical",
            "history": {"entitiesByKey": {}, "islands": [{"entries": []}]},
        }
    return state


async def insert_terminal(owner, adapter, *, paged, status, patches):
    turn = {
        "turnId": "accepted",
        "status": status,
        "items": [{"id": "result", "type": "agentMessage", "text": "actual result"}],
        "nativeExtra": "preserved",
    }
    if patches:
        changes = (
            [
                {
                    "op": "add",
                    "path": ["turnHistory", "history", "entitiesByKey", "accepted"],
                    "value": turn,
                },
                {
                    "op": "add",
                    "path": ["turnHistory", "history", "islands", 0, "entries", 0],
                    "value": {"key": "accepted", "value": "accepted"},
                },
            ]
            if paged
            else [{"op": "add", "path": ["turns", 0], "value": turn}]
        )
        revision = await owner.publish_patches("remote", changes)
    else:
        state = owner.get_state("remote")
        if paged:
            state["turnHistory"]["history"]["entitiesByKey"]["accepted"] = turn
            state["turnHistory"]["history"]["islands"][0]["entries"].append(
                {"key": "accepted", "value": "accepted"}
            )
        else:
            state["turns"].append(turn)
        revision = await owner.publish_state("remote", state)
    await adapter.peer.wait_revision("remote", revision, timeout=2)
    return deepcopy(turn)


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted", "cancelled"])
@pytest.mark.parametrize("paged", [False, True])
@pytest.mark.parametrize("before_ack", [False, True])
@pytest.mark.parametrize("patches", [False, True])
@async_test
async def test_same_target_terminal_defeats_new_or_retained_ack(
    tmp_path, status, paged, before_ack, patches
):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, owner, native):
        await owner.claim("remote", initial_state(paged))
        await runtime.prepare_session_view("view", "remote")
        calls = []

        async def consume(method, params):
            calls.append((method, params))
            if before_ack:
                await insert_terminal(
                    owner, adapter, paged=paged, status=status, patches=patches
                )
            return {
                "result": {
                    "turn": {"id": "accepted", "status": "inProgress", "items": []}
                }
            }

        owner.owner_handler = consume
        result = await runtime.start_turn("view", "remote", "fresh input")
        assert result.ok and result.result["turnId"] == "accepted"
        if not before_ack:
            assert runtime._active_turn_ids.get("view") == "accepted"
            await insert_terminal(
                owner, adapter, paged=paged, status=status, patches=patches
            )
        await adapter.refresh_state("remote", force=True)
        assert runtime._session_states.get("view").status == "idle"
        assert "view" not in runtime._active_turn_ids
        assert "remote" not in adapter.ack_activity
        observed = await adapter.read_thread("remote")
        assert thread_status(observed.thread) == "idle"
        assert observed.thread["turns"][0]["status"] == status
        assert enumerate_turns(adapter.peer.get_state("remote")) == enumerate_turns(
            owner.get_state("remote")
        )
        assert observed.thread["turns"][0]["nativeExtra"] == "preserved"
        assert len(calls) == 1 and native.calls == []


@pytest.mark.parametrize("paged", [False, True])
@async_test
async def test_target_terminal_keeps_other_native_active_evidence(tmp_path, paged):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, owner, native):
        await owner.claim("remote", initial_state(paged))
        await runtime.prepare_session_view("view", "remote")

        async def consume(method, params):
            await insert_terminal(
                owner, adapter, paged=paged, status="completed", patches=True
            )
            state = owner.get_state("remote")
            state["threadRuntimeStatus"] = {"type": "active"}
            state["turns"].append(
                {"turnId": "other-active", "status": "inProgress", "items": []}
            )
            revision = await owner.publish_state("remote", state)
            await adapter.peer.wait_revision("remote", revision, timeout=2)
            return {
                "result": {
                    "turn": {"id": "accepted", "status": "inProgress", "items": []}
                }
            }

        owner.owner_handler = consume
        await runtime.start_turn("view", "remote", "fresh input")
        assert runtime._active_turn_ids.get("view") == "other-active"
        assert runtime._session_states.get("view").status == "running"
        assert "remote" not in adapter.ack_activity
        assert native.calls == []
