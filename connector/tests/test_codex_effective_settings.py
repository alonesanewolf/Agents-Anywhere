"""Native acceptance regressions through the production runtime and owner paths."""

import pytest
from test_codex_commands import setup as command_setup
from test_codex_coordination_operations import async_test
from test_codex_runtime import FakeCodexClient
from test_codex_runtime_coordination_review import real_runtime

from connector.runtimes.codex.coordination.wire import IpcError
from connector.runtimes.codex.sdk.runtime_client import CodexStartThreadRequest


def envelope():
    return {
        "thread": {
            "id": "t",
            "turns": [],
            "model": "raw-model",
            "reasoningEffort": "high",
            "cwd": "/repo",
        },
        "model": "gpt-example",
        "reasoningEffort": "max",
        "modelProvider": "openai",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "cwd": "/repo",
        "sandbox": {
            "type": "workspaceWrite",
            "writableRoots": [],
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        },
        "serviceTier": None,
        "activePermissionProfile": None,
        "runtimeWorkspaceRoots": [],
    }


def configure_resume(native, *, effort="max", instructions=None, response_omit=()):
    """Known cold authority; output settings are applied from the serialized wire.

    Null service tier and mode instructions are explicit independent native
    defaults, since the SDK omits a null serviceTier and resume has no mode field.
    This models a compatible native process, not installed Plan/null support.
    """
    fixture = native.configure_resume(
        "t",
        cwd="/repo",
        settings={
            "model": "gpt-example",
            "model_provider_id": "openai",
            "reasoning_effort": effort,
            "service_tier": None,
            "collaboration_mode": {
                "mode": "default",
                "settings": {
                    "model": "gpt-example",
                    "reasoning_effort": effort,
                    "developer_instructions": instructions,
                },
            },
        },
        native_defaults={
            "service_tier": None,
            "collaboration_mode": {
                "mode": "default",
                "developer_instructions": instructions,
            },
        },
        thread_fields={"model": "raw-model", "reasoningEffort": "high"},
        response_omit=response_omit,
    )
    native.responses["thread/resume"] = {"activePermissionProfile": None}
    return fixture


async def settings_event(adapter, values):
    await adapter._native_event(
        {
            "method": "thread/settings/updated",
            "params": {"threadId": "t", "threadSettings": values},
        },
        1,
    )


async def configure(runtime, native):
    native.list_models = FakeCodexClient().list_models
    runtime._lifecycle.model_list_result = await native.list_models()
    model = (
        (await runtime.list_model_catalog()).models[0].reasoning_items[0].selection_id
    )
    permission = (
        (await runtime.list_permission_catalog(query="request"))
        .permissions[0]
        .selection_id
    )
    return {"model": model, "permission": permission}


@pytest.mark.parametrize("completed", [False, True])
@async_test
async def test_create_first_turn_keeps_explicit_selection_and_effective_projection(
    tmp_path, completed
):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        selections = await configure(runtime, native)

        async def start_thread(request):
            return envelope()

        native.native_thread_start = start_thread

        async def start_turn(request):
            if completed:
                await native.handler(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "t",
                            "turn": {"id": "one", "status": "completed", "items": []},
                        },
                    },
                    1,
                )
            return {"turn": {"id": "one", "status": "inProgress", "items": []}}

        native.responses["turn/start"] = start_turn
        result = await runtime.create_and_start_session(
            "s", "hello", cwd="/repo", selections=selections
        )
        assert result.ok
        request = next(p for m, p in native.calls if m == "turn/start")
        assert request["model"] == "gpt-example"
        assert request["effort"] == "low"
        assert request["approvalPolicy"] == "on-request"
        assert request["sandboxPolicy"]["type"] == "workspaceWrite"
        await adapter.peer._notifications.join()
        state = adapter.peer.get_state("t")
        assert state["latestModel"] == "gpt-example"
        assert state["latestReasoningEffort"] == "low"
        assert state["currentPermissions"]["approvalPolicy"] == "on-request"
        assert runtime._session_states.get("s").selections == selections
        assert (
            next(
                c for c in await runtime.list_commands("s", "t") if c.id == "plan"
            ).disabled_reason
            != "native_model_unknown"
        )


@pytest.mark.parametrize("seam", ["start", "resume", "read"])
@async_test
async def test_effective_envelope_settings_are_canonical(tmp_path, seam):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        await configure(runtime, native)
        raw = envelope()

        async def start_thread(request):
            return raw

        native.native_thread_start = start_thread
        configure_resume(native)
        if seam == "start":
            await adapter.start_thread(CodexStartThreadRequest())
        elif seam == "resume":
            await adapter._acquire("t")
        else:
            raw["thread"]["turns"] = [
                {"id": "explicit-history", "status": "completed", "items": []}
            ]
            native.responses["thread/read"] = {"thread": raw["thread"]}
            thread = (await adapter.read_thread("t")).thread
            assert thread["latestModel"] == "raw-model"
            assert thread["latestReasoningEffort"] == "high"
            assert len(thread["turns"]) == 1
            assert thread["turns"][0]["id"] == "explicit-history"
            assert thread["turns"][0]["status"] == "completed"
            assert thread["turns"][0]["items"] == []
            assert "currentPermissions" not in thread
            assert [m for m, _ in native.calls] == ["thread/read"]
            return
        state = adapter.peer.get_state("t")
        assert state["latestModel"] == "gpt-example"
        assert state["latestReasoningEffort"] == "max"
        assert state["latestThreadSettings"]["serviceTier"] is None
        assert state["currentPermissions"]["sandboxPolicy"] == raw["sandbox"]
        assert state["currentPermissions"]["activePermissionProfile"] is None
        assert (await runtime._session_reader.selections_from_thread(state))[
            "permission"
        ] is not None


@pytest.mark.parametrize("values", [{}, {"reasoningEffort": None}])
@async_test
async def test_unknown_fields_stay_absent_and_explicit_null_is_observed(
    tmp_path, values
):
    async with real_runtime(tmp_path) as (_runtime, _host, adapter, _owner, native):

        async def start_thread(request):
            return {"thread": {"id": "t", "turns": []}, **values}

        native.native_thread_start = start_thread
        await adapter.start_thread(CodexStartThreadRequest())
        state = adapter.peer.get_state("t")
        assert "latestModel" not in state
        assert "currentPermissions" not in state
        assert ("latestReasoningEffort" in state) == ("reasoningEffort" in values)
        if values:
            assert state["latestReasoningEffort"] is None


@pytest.mark.parametrize("seam", ["start", "resume"])
@pytest.mark.parametrize("boundary", ["native", "publish"])
@async_test
async def test_settings_events_win_during_owner_initialization(
    tmp_path, seam, boundary
):
    async with real_runtime(tmp_path) as (_runtime, _host, adapter, _owner, native):

        async def reply(_):
            if boundary == "native":
                await settings_event(adapter, {"model": "new", "effort": "low"})
            return envelope()

        native.native_thread_start = reply
        configure_resume(native)

        async def resume_reply(_):
            if boundary == "native":
                await settings_event(adapter, {"model": "new", "effort": "low"})
            return {}

        native.responses["thread/resume"] = resume_reply
        original = adapter.peer.client.broadcast
        fired = False

        async def broadcast(method, params, **kwargs):
            nonlocal fired
            await original(method, params, **kwargs)
            if (
                boundary == "publish"
                and method == "thread-stream-state-changed"
                and not fired
            ):
                fired = True
                await settings_event(adapter, {"model": "new", "effort": "low"})

        adapter.peer.client.broadcast = broadcast
        if seam == "start":
            await adapter.start_thread(CodexStartThreadRequest())
        else:
            await adapter._acquire("t")
        state = adapter.peer.get_state("t")
        assert state["latestModel"] == "new"
        assert state["latestReasoningEffort"] == "low"


@pytest.mark.parametrize("operation", ["turn", "settings", "selection"])
@pytest.mark.parametrize("boundary", ["native", "publish"])
@async_test
async def test_later_settings_event_wins_over_owner_ack(tmp_path, operation, boundary):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        selections = await configure(runtime, native)
        configure_resume(native)
        await adapter._acquire("t")
        await runtime._session_states.update("s", "t", status="idle")

        async def reply(_):
            if boundary == "native":
                await settings_event(
                    adapter, {"model": "gpt-example", "effort": "high"}
                )
            return (
                {"turn": {"id": "one", "status": "inProgress"}}
                if operation == "turn"
                else {}
            )

        native.responses[
            "turn/start" if operation == "turn" else "thread/settings/update"
        ] = reply
        broadcast = adapter.peer.client.broadcast
        fired = False

        async def race(method, params, **kwargs):
            nonlocal fired
            await broadcast(method, params, **kwargs)
            if (
                boundary == "publish"
                and method == "thread-stream-state-changed"
                and not fired
            ):
                fired = True
                await settings_event(
                    adapter, {"model": "gpt-example", "effort": "high"}
                )

        adapter.peer.client.broadcast = race
        if operation == "turn":
            await runtime.start_turn("s", "t", "hello", selections=selections)
        elif operation == "selection":
            await runtime.update_session_selections("s", "t", selections)
        else:
            await adapter.command_owner_operation(
                "t",
                "thread-follower-update-thread-settings",
                {"threadSettings": {"model": "gpt-example", "effort": "low"}},
            )
            await adapter.refresh_state("t", force=True)
        assert adapter.peer.get_state("t")["latestReasoningEffort"] == "high"
        assert (
            runtime._session_states.get("s").selections["model"] != selections["model"]
        )


@pytest.mark.parametrize("operation", ["turn", "settings"])
@pytest.mark.parametrize("error", [TimeoutError("unknown"), ValueError("rejected")])
@async_test
async def test_failed_or_unknown_ack_does_not_apply_requested_settings(
    tmp_path, operation, error
):
    async with real_runtime(tmp_path) as (_runtime, _host, adapter, _owner, native):
        configure_resume(native)
        await adapter._acquire("t")
        before = adapter.peer.get_state("t")
        native.responses[
            "turn/start" if operation == "turn" else "thread/settings/update"
        ] = error
        with pytest.raises(type(error)):
            await adapter.command_owner_operation(
                "t",
                "thread-follower-start-turn"
                if operation == "turn"
                else "thread-follower-update-thread-settings",
                {
                    "turnStart": {
                        "request": {"threadId": "t", "input": [], "effort": "low"}
                    }
                }
                if operation == "turn"
                else {"threadSettings": {"effort": "low"}},
            )
        assert adapter.peer.get_state("t") == before


@pytest.mark.parametrize("switch,mode", [("on", "plan"), ("off", "default")])
@async_test
async def test_plan_switch_resets_old_instructions_and_keeps_observed_model_effort(
    switch, mode
):
    runtime, native = await command_setup()
    await runtime._session_states.update(
        "s",
        "t",
        status="idle",
        metadata={
            "codexSettings": {
                "latestModel": "gpt-example",
                "latestReasoningEffort": "low",
                "latestThreadSettings": {
                    "collaborationMode": {
                        "mode": "default" if mode == "plan" else "plan",
                        "settings": {
                            "model": "old",
                            "reasoning_effort": "max",
                            "developer_instructions": "Generated instructions for previous mode",
                        },
                    }
                },
            }
        },
    )
    result = await runtime.execute_command("s", "plan", "t", args=(switch,))
    assert result.ok
    assert native.calls[-1][1]["threadSettings"]["collaborationMode"] == {
        "mode": mode,
        "settings": {
            "model": "gpt-example",
            "reasoning_effort": "low",
            "developer_instructions": None,
        },
    }


@async_test
async def test_native_negative_settings_ack_does_not_change_canonical(tmp_path):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        selections = await configure(runtime, native)
        configure_resume(native)
        await adapter._acquire("t")
        await runtime._session_states.update("s", "t", status="idle")
        await adapter.refresh_state("t", force=True)
        before = adapter.peer.get_state("t")
        display = runtime._session_states.get("s").selections
        native.responses["thread/settings/update"] = {"applied": False}
        result = await runtime.update_session_selections("s", "t", selections)
        assert not result.ok
        assert adapter.peer.get_state("t") == before
        assert runtime._session_states.get("s").selections == display


@async_test
async def test_followed_send_uses_new_owner_snapshot_not_aa_selection_cache(tmp_path):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, owner, native):
        selections = await configure(runtime, native)
        forwarded = []

        async def operation(method, params):
            forwarded.append(params)
            return {"result": {"turn": {"id": "remote", "status": "inProgress"}}}

        owner.owner_handler = operation
        await owner.claim(
            "t",
            {
                "id": "t",
                "turns": [],
                "requests": [],
                "latestModel": "gpt-example",
                "latestReasoningEffort": "high",
                "latestThreadSettings": {"model": "gpt-example", "effort": "high"},
            },
        )
        await runtime._session_states.update(
            "s", "t", status="idle", selections=selections
        )
        await adapter.attach_thread("t")
        await adapter.refresh_state("t", force=True)
        await runtime.start_turn("s", "t", "no new selection")
        request = forwarded[0]["turnStart"]["request"]
        assert "model" not in request and "effort" not in request
        assert forwarded[0]["turnStart"]["context"]["inheritThreadSettings"] is True
        assert (
            runtime._session_states.get("s").selections["model"] != selections["model"]
        )
        assert adapter.peer.get_state("t")["latestReasoningEffort"] == "high"
        assert native.calls == []


@async_test
async def test_confirmed_turn_updates_embedded_mode_effort_without_inventing_fields(
    tmp_path,
):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        selections = await configure(runtime, native)
        configure_resume(native, instructions="default mode")
        await adapter._acquire("t")
        await runtime._session_states.update("s", "t", status="idle")
        native.responses["turn/start"] = {"turn": {"id": "one", "status": "inProgress"}}
        await runtime.start_turn("s", "t", "hello", selections=selections)
        state = adapter.peer.get_state("t")
        assert state["latestCollaborationMode"]["settings"]["reasoning_effort"] == "low"
        assert (
            state["latestThreadSettings"]["collaborationMode"]
            == state["latestCollaborationMode"]
        )
        assert (
            state["latestThreadSettings"]["collaborationMode"]["settings"][
                "developer_instructions"
            ]
            == "default mode"
        )


@pytest.mark.parametrize("error", [TimeoutError("unknown"), ValueError("rejected")])
@async_test
async def test_runtime_failed_start_restores_known_canonical_selection(tmp_path, error):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        selections = await configure(runtime, native)
        configure_resume(native, effort="high")
        await adapter._acquire("t")
        await runtime._session_states.update("s", "t", status="idle")
        await adapter.refresh_state("t", force=True)
        before = dict(runtime._session_states.get("s").selections)
        native.responses["turn/start"] = error
        with pytest.raises(type(error)):
            await runtime.start_turn("s", "t", "hello", selections=selections)
        assert runtime._session_states.get("s").selections == before
        assert adapter.peer.get_state("t")["latestReasoningEffort"] == "high"
        assert [m for m, _ in native.calls].count("turn/start") == 1


@pytest.mark.parametrize("switch,mode", [("on", "plan"), ("off", "default")])
@pytest.mark.parametrize(
    "mode_present", [False, True], ids=["absent-mode", "null-mode"]
)
@pytest.mark.parametrize("effort", ["low", None], ids=["low", "null-effort"])
@async_test
async def test_observed_null_or_missing_mode_can_switch_plan(
    tmp_path, switch, mode, mode_present, effort
):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        await configure(runtime, native)
        observed = {
            **envelope(),
            "reasoningEffort": effort,
            **({"collaborationMode": None} if mode_present else {}),
        }

        async def start_thread(_):
            return observed

        native.native_thread_start = start_thread
        await adapter.start_thread(CodexStartThreadRequest())
        await runtime._session_states.update("s", "t", status="idle")
        await adapter.refresh_state("t", force=True)
        before = adapter.peer.get_state("t")
        assert ("collaborationMode" in before["latestThreadSettings"]) is mode_present
        assert before["latestThreadSettings"].get("collaborationMode") is None
        assert before["latestReasoningEffort"] == effort

        result = await runtime.execute_command("s", "plan", "t", args=(switch,))

        assert result.ok, result
        assert native.calls[-1] == (
            "thread/settings/update",
            {
                "threadId": "t",
                "collaborationMode": {
                    "mode": mode,
                    "settings": {
                        "model": "gpt-example",
                        "reasoning_effort": effort,
                        "developer_instructions": None,
                    },
                },
            },
        )
        current = adapter.peer.get_state("t")
        assert current["latestReasoningEffort"] == effort
        assert current["currentPermissions"] == before["currentPermissions"]
        assert current["latestThreadSettings"]["serviceTier"] is None


@pytest.mark.parametrize(
    "mode_present", [False, True], ids=["absent-mode", "null-mode"]
)
@pytest.mark.parametrize("effort", ["low", None], ids=["low", "null-effort"])
@async_test
async def test_resume_authority_restores_mode_over_null_or_missing_envelope(
    tmp_path, mode_present, effort
):
    async with real_runtime(tmp_path) as (_runtime, _host, adapter, _owner, native):
        fixture = configure_resume(
            native,
            effort=effort,
            response_omit=() if mode_present else ("collaborationMode",),
        )
        if mode_present:
            native.responses["thread/resume"]["collaborationMode"] = None
        await adapter._acquire("t")
        state = adapter.peer.get_state("t")
        assert state["latestReasoningEffort"] == effort
        assert state["latestThreadSettings"]["collaborationMode"] == {
            "mode": "default",
            "settings": {
                "model": "gpt-example",
                "reasoning_effort": effort,
                "developer_instructions": None,
            },
        }
        assert state["latestThreadSettings"]["serviceTier"] is None
        wire = next(p for m, p in fixture.calls if m == "thread/resume")
        assert wire["config"]["model_reasoning_effort"] == effort
        assert wire["serviceTier"] is None
        assert "collaborationMode" not in wire
        assert not [p for m, p in native.calls if m == "turn/start"]


@pytest.mark.parametrize(
    "override",
    [
        {"model": "wrong-model"},
        {"cwd": "/other"},
        {"sandbox": {"type": "dangerFullAccess"}},
    ],
)
@async_test
async def test_explicit_mismatching_resume_response_still_fails_closed(
    tmp_path, override
):
    async with real_runtime(tmp_path) as (_runtime, _host, adapter, _owner, native):
        configure_resume(native)
        native.responses["thread/resume"] = override
        with pytest.raises(IpcError, match="codex_resume_effective_settings_mismatch"):
            await adapter._acquire("t")
        assert [m for m, _ in native.calls] == ["thread/read", "thread/resume"]
        assert not adapter.peer.is_owner("t")
        assert not adapter.acquiring
