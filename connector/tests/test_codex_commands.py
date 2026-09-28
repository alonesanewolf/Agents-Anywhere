"""Public runtime commands, with a non-mutating native boundary."""

import asyncio
from copy import deepcopy

import pytest
from test_codex_runtime import FakeCodexClient, FakeHost

from connector.runtime_protocol import RuntimeConfig
from connector.runtimes.codex.runtime import CodexRuntime


class CommandsClient(FakeCodexClient):
    def __init__(self, role="owner"):
        super().__init__()
        self.role = role
        self.calls = []
        self.goal = None
        self.error = None

    def command_capabilities(self, thread_id):
        return {"role": self.role, "coordinated": True, "nativeControls": True}

    async def native_request(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if self.error:
            raise self.error
        if method == "thread/goal/get":
            return {"goal": self.goal}
        if method == "thread/goal/set":
            self.goal = {**(self.goal or {}), **params}
            return {"goal": self.goal}
        if method == "thread/goal/clear":
            self.goal = None
            return {"cleared": True}
        if method == "review/start":
            return {"turn": {"id": "physical-review"}, "reviewThreadId": "t"}
        return {}

    async def observe_goal(self, thread_id, goal):
        self.observed = goal

    async def owner_operation(self, thread_id, method, payload):
        self.calls.append((method, deepcopy(payload)))
        return (
            {"applied": True}
            if method.endswith("update-thread-settings")
            else {"ok": True}
        )

    async def stop_session(self, thread_id):
        self.calls.append(("stop_session", thread_id))
        return {"ok": True, "interruptedTurnId": "physical"}


async def setup(role="owner", status="idle"):
    host, client = FakeHost(), CommandsClient(role)
    runtime = CodexRuntime(
        RuntimeConfig(runtime="codex", revision=8, values={}), host, client
    )
    await runtime._session_states.update(
        "s",
        "t",
        status=status,
        metadata={
            "codexCoordination": {"role": role, "available": role != "offline"},
            "codexSettings": {"latestModel": "gpt-test"},
        },
    )
    return runtime, client


def run(fn):
    return asyncio.run(fn())


def test_catalog_owner_follower_none_busy_and_query():
    async def scenario():
        for role in ("owner", "follower", "none", "offline"):
            runtime, client = await setup(role)
            commands = {c.id: c for c in await runtime.list_commands("s", "t")}
            assert commands["compact"].enabled is (role in {"owner", "follower"})
            assert commands["review"].enabled is (role == "owner")
            assert commands["goal"].metadata["goalActions"]["create"]["enabled"] is (
                role == "owner"
            )
            assert commands["model"].metadata["ui"] == {
                "kind": "selector",
                "target": "model",
            }
            assert commands["goal"].args_schema == {"type": "string"}
            assert not client.calls
        runtime, _ = await setup(status="running")
        commands = {c.id: c for c in await runtime.list_commands("s", "t")}
        assert not commands["compact"].enabled
        assert not commands["goal"].metadata["goalActions"]["pause"]["enabled"]
        assert len(await runtime.list_commands("s", "t", query="GOAL", limit=1)) == 1

    run(scenario)


def test_compact_acceptance_and_selector_no_rpc():
    async def scenario():
        runtime, client = await setup()
        result = await runtime.execute_command("s", "compact", "t", raw="/compact")
        assert result.ok and result.result["executionState"] == "accepted"
        assert client.calls == [("thread-follower-compact-thread", {})]
        client.calls.clear()
        result = await runtime.execute_command("s", "model", "t")
        assert not result.ok and result.code == "command_requires_selector"
        assert client.calls == []

    run(scenario)


def test_goal_lifecycle_budget_completed_clear_and_physical_end():
    async def scenario():
        runtime, client = await setup()
        for text, expected in [
            (
                'create {"objective":"do things","tokenBudget":200}',
                {"objective": "do things", "tokenBudget": 200, "status": "active"},
            ),
            ("edit better objective", {"objective": "better objective"}),
            ("pause", {"status": "paused"}),
            ("resume", {"status": "active"}),
            ("budget null", {"tokenBudget": None}),
            ('set {"status":"complete"}', {"status": "complete"}),
        ]:
            result = await runtime.execute_command(
                "s", "goal", "t", raw="/goal " + text
            )
            assert result.ok, result
            assert client.calls[-1] == (
                "thread/goal/set",
                {"threadId": "t", **expected},
            )
        await runtime.execute_command("s", "goal", "t", args=("clear",))
        state = runtime._session_states.get("s")
        assert state.metadata["codexPresentation"]["threadGoal"] is None
        assert (
            state.metadata["codexPresentation"]["completedThreadGoal"]["status"]
            == "complete"
        )

    run(scenario)


@pytest.mark.parametrize(
    "command,raw,args",
    [
        ("goal", "", ()),
        ("goal", "/compact", ()),
        ("compact", "/compact extra", ()),
        ("goal", '/goal set {"threadId":"other"}', ()),
        ("goal", '/goal create {"objective":"x","tokenBudget":true}', ()),
        ("goal", '/goal set {"status":"oops"}', ()),
        ("goal", None, ("create", "split", "objective")),
    ],
)
def test_invalid_input_never_dispatches(command, raw, args):
    async def scenario():
        runtime, client = await setup()
        result = await runtime.execute_command("s", command, "t", raw=raw, args=args)
        assert not result.ok and result.code == "invalid_command"
        assert not client.calls

    run(scenario)


def test_followed_goal_mutation_and_review_never_use_native_fallback():
    async def scenario():
        for role in ("follower", "none"):
            runtime, client = await setup(role)
            for command, args in [("goal", ("create objective",)), ("review", ())]:
                result = await runtime.execute_command("s", command, "t", args=args)
                assert not result.ok
            assert not client.calls

    run(scenario)


def test_review_and_plan_use_explicit_native_parameters():
    async def scenario():
        runtime, client = await setup()
        result = await runtime.execute_command("s", "review", "t")
        assert result.ok and result.result["executionState"] == "accepted"
        assert client.calls[-1] == (
            "review/start",
            {
                "threadId": "t",
                "target": {"type": "uncommittedChanges"},
                "delivery": "inline",
            },
        )
        result = await runtime.execute_command("s", "plan", "t", args=("off",))
        assert result.ok
        assert client.calls[-1] == (
            "thread-follower-update-thread-settings",
            {
                "threadSettings": {
                    "collaborationMode": {
                        "mode": "default",
                        "settings": {
                            "model": "gpt-test",
                            "developer_instructions": None,
                        },
                    }
                }
            },
        )

    run(scenario)


def test_goal_user_stop_between_turns_uses_session_stop():
    async def scenario():
        runtime, client = await setup()
        await runtime._session_states.update(
            "s",
            "t",
            status="idle",
            metadata={"codexPresentation": {"threadGoal": {"status": "active"}}},
        )
        result = await runtime.interrupt_session("s")
        assert result.ok
        assert client.calls == [("stop_session", "t")]
        capabilities = await runtime.get_session_capabilities("s", "t")
        assert next(
            c
            for c in capabilities.capabilities
            if c.capability_id == "session.commands"
        ).supported

    run(scenario)


def test_catalog_revision_and_goal_stop_availability_change_with_metadata():
    async def scenario():
        runtime, _ = await setup()
        before = await runtime.get_session_capabilities("s", "t")
        await runtime._session_states.update(
            "s",
            "t",
            status="idle",
            metadata={
                "codexPresentation": {
                    "threadGoal": {
                        "threadId": "t",
                        "objective": "work",
                        "status": "active",
                    }
                }
            },
        )
        after = await runtime.get_session_capabilities("s", "t")

        def capability(value, name):
            return next(c for c in value.capabilities if c.capability_id == name)

        assert capability(after, "session.interrupt").available
        assert (
            capability(before, "session.commands").metadata["catalogRevision"]
            != capability(after, "session.commands").metadata["catalogRevision"]
        )

    run(scenario)


def test_partial_goal_pause_is_failure_without_false_idle():
    async def scenario():
        runtime, client = await setup(status="running")
        runtime._active_turn_ids["s"] = "physical"

        async def stop(_):
            return {
                "ok": True,
                "interruptedTurnId": "physical",
                "goalPauseError": "unknown",
            }

        client.stop_session = stop
        result = await runtime.interrupt_session("s")
        assert not result.ok and result.result["executionState"] == "unknown"
        assert runtime._session_states.get("s").status == "running"

    run(scenario)


def test_native_goal_failure_unknown_and_no_replay():
    async def scenario():
        runtime, client = await setup()
        client.error = TimeoutError("secret")
        result = await runtime.execute_command(
            "s", "goal", "t", args=("create objective",)
        )
        assert not result.ok and result.result == {
            "executionState": "unknown",
            "retryable": False,
        }
        assert len(client.calls) == 1
        assert "secret" not in str(result)

    run(scenario)


def test_sdk_goal_notifications_hydrate_without_physical_turn_completion():
    async def scenario():
        runtime, _ = await setup()
        goal = {
            "threadId": "t",
            "objective": "work",
            "status": "active",
            "tokenBudget": 10,
        }
        await runtime._notifications.handle(
            {"method": "thread/goal/updated", "params": {"threadId": "t", "goal": goal}}
        )
        assert (
            runtime._session_states.get("s").metadata["codexPresentation"]["threadGoal"]
            == goal
        )
        await runtime._notifications.handle(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "t",
                    "turn": {"id": "physical", "status": "completed", "items": []},
                },
            }
        )
        assert (
            runtime._session_states.get("s").metadata["codexPresentation"][
                "threadGoal"
            ]["status"]
            == "active"
        )

    run(scenario)


@pytest.mark.parametrize(
    "command,text",
    [
        ("settings", '{"threadSettings":{"threadId":"other"}}'),
        ("settings", '{"wrong":1}'),
        ("queue", '{"state":{"other":[]}}'),
        ("edit", '{"turnId":"p"}'),
        ("daybreak", '{"daybreakEnabled":"true"}'),
    ],
)
def test_malformed_control_payload_is_not_dispatched(command, text):
    async def scenario():
        runtime, client = await setup()
        result = await runtime.execute_command("s", command, "t", args=(text,))
        assert result.code == "invalid_command"
        assert not client.calls

    run(scenario)


def test_typed_sdk_goal_events_preserve_budget_and_explicit_clear():
    from openai_codex.generated.v2_all import (
        ThreadGoalClearedNotification,
        ThreadGoalUpdatedNotification,
    )
    from openai_codex.models import Notification

    async def scenario():
        runtime, _ = await setup()
        goal = {
            "threadId": "t",
            "objective": "done",
            "status": "complete",
            "tokenBudget": None,
            "createdAt": 1,
            "updatedAt": 2,
            "timeUsedSeconds": 3,
            "tokensUsed": 4,
        }
        event = Notification(
            "thread/goal/updated",
            ThreadGoalUpdatedNotification.model_validate(
                {"threadId": "t", "goal": goal}
            ),
        )
        await runtime._notifications.handle(event)
        assert (
            runtime._session_states.get("s").metadata["codexPresentation"]["threadGoal"]
            == goal
        )
        await runtime._notifications.handle(
            Notification(
                "thread/goal/cleared", ThreadGoalClearedNotification(threadId="t")
            )
        )
        presentation = runtime._session_states.get("s").metadata["codexPresentation"]
        assert presentation["threadGoal"] is None
        assert presentation["completedThreadGoal"] == goal

    run(scenario)


def test_goal_actions_follow_known_native_status():
    async def scenario():
        runtime, client = await setup()
        for status in (
            "active",
            "paused",
            "blocked",
            "usageLimited",
            "budgetLimited",
            "complete",
        ):
            goal = {"threadId": "t", "objective": "work", "status": status}
            await runtime._session_states.update(
                "s",
                "t",
                status="idle",
                metadata={"codexPresentation": {"threadGoal": goal}},
            )
            descriptor = next(
                c for c in await runtime.list_commands("s", "t") if c.id == "goal"
            )
            assert descriptor.metadata["goalActions"]["pause"]["enabled"] is (
                status == "active"
            )
            assert descriptor.metadata["goalActions"]["resume"]["enabled"] is (
                status not in {"active", "complete"}
            )
        result = await runtime.execute_command("s", "goal", "t", args=("pause",))
        assert not result.ok and not client.calls

    run(scenario)


def test_invalid_native_command_ack_is_unknown():
    async def scenario():
        runtime, client = await setup()

        async def operation(*_):
            return {}

        client.owner_operation = operation
        result = await runtime.execute_command("s", "compact", "t")
        assert not result.ok and result.result["executionState"] == "unknown"

    run(scenario)


def test_offline_command_returns_known_failure_without_dispatch():
    async def scenario():
        runtime, client = await setup("offline")
        result = await runtime.execute_command("s", "compact", "t")
        assert not result.ok and result.code == "command_unavailable"
        assert not client.calls

    run(scenario)


def test_unobserved_goal_status_is_not_empty_success():
    async def scenario():
        runtime, client = await setup("follower")
        result = await runtime.execute_command("s", "goal", "t")
        assert not result.ok
        assert not client.calls

    run(scenario)


def test_default_sdk_commands_require_real_local_load_and_live_lifecycle():
    from test_codex_sdk_client import _FakeLowLevelAsyncCodex, _FakeLowLevelSdkModule

    from connector.runtimes.codex.sdk.client import CodexSdkClient
    from connector.runtimes.codex.sdk.runtime_client import CodexStartThreadRequest

    async def scenario():
        native = _FakeLowLevelAsyncCodex()
        client = CodexSdkClient(native, sdk=_FakeLowLevelSdkModule())
        runtime = CodexRuntime(
            RuntimeConfig(runtime="codex", revision=8, values={}), FakeHost(), client
        )
        await runtime.start()
        assert client.command_capabilities("thread_1")["role"] == "none"
        result = await client.start_thread(CodexStartThreadRequest())
        thread_id = result.thread_id
        await runtime._session_states.update("s", thread_id, status="idle")
        commands = {c.id: c for c in await runtime.list_commands("s", thread_id)}
        assert commands["compact"].enabled
        assert not commands["review"].enabled
        assert (
            commands["goal"].metadata["goalActions"]["create"]["disabledReason"]
            == "native_controls_unavailable"
        )
        await runtime.stop()
        assert client.command_capabilities(thread_id)["role"] == "none"

    run(scenario)


def test_default_sdk_known_active_goal_does_not_offer_incomplete_stop():
    async def scenario():
        runtime, _ = await setup()
        runtime.client.stop_session = None
        runtime._active_turn_ids["s"] = "physical"
        await runtime._session_states.update(
            "s",
            "t",
            status="running",
            metadata={"codexPresentation": {"threadGoal": {"status": "active"}}},
        )
        capabilities = await runtime.get_session_capabilities("s", "t")
        interrupt = next(
            c
            for c in capabilities.capabilities
            if c.capability_id == "session.interrupt"
        )
        assert not interrupt.available
        result = await runtime.interrupt_session("s")
        assert not result.ok and result.code == "goal_stop_unavailable"

    run(scenario)
