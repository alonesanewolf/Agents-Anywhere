"""AA integration behavior; fake native boundary, no installed-app mutations."""

import asyncio

import pytest
from test_codex_runtime import FakeCodexClient, FakeHost

from connector.runtime_protocol import RuntimeConfig
from connector.runtimes.codex.runtime import CodexRuntime
from connector.runtimes.codex.sdk.runtime_client import CodexThreadReadResult


class CoordinatedFake(FakeCodexClient):
    def __init__(self):
        super().__init__()
        self.attached = set()
        self.detached = []
        self.complete = False
        self.thread = {"id": "thread_1", "turns": [], "model": "owner-model"}
        self.tokens = []

    async def attach_thread(self, thread_id):
        self.attached.add(thread_id)

    async def detach_thread(self, thread_id):
        self.attached.discard(thread_id)
        self.detached.append(thread_id)

    async def refresh_state(self, thread_id, **kwargs):
        await self.handler(
            event(self.thread, complete=self.complete, thread_id=thread_id)
        )

    def is_follower(self, thread_id):
        return thread_id in self.attached

    def has_canonical_authority(self, thread_id):
        return thread_id in self.attached

    async def read_thread(self, thread_id, include_turns=True):
        return CodexThreadReadResult(
            thread=self.thread,
            canonical_complete=self.complete,
            coordination_role="follower",
        )

    async def respond_to_request(self, token, result):
        self.tokens.append((token, result))
        if self.response_error:
            raise self.response_error


def event(
    thread,
    *,
    complete=False,
    token=None,
    method="item/commandExecution/requestApproval",
    params=None,
    thread_id="thread_1",
):
    request = {
        "id": 7,
        "method": method,
        "params": {"threadId": thread_id, "command": "ls", **(params or {})},
    }
    return {
        "method": "coordination/state",
        "params": {
            "threadId": thread_id,
            "thread": thread,
            "canonicalComplete": complete,
            "requests": [request] if token else [],
            "requestContexts": [
                {
                    "threadId": thread_id,
                    "requestId": 7,
                    "method": method,
                    "responseContext": token,
                }
            ]
            if token
            else [],
            "coordination": {
                "role": "follower",
                "ownerClientId": "app" if thread else None,
            },
            "presentation": {
                "threadGoal": thread.get("threadGoal"),
                "completedThreadGoal": None,
            }
            if thread
            else None,
            "capabilities": {"role": "follower", "goalControl": False},
        },
    }


def setup():
    host, client = FakeHost(), CoordinatedFake()
    runtime = CodexRuntime(
        RuntimeConfig(runtime="codex", revision=8, values={"appIntegration": True}),
        host,
        client,
    )
    return runtime, host, client


def turn(id, text):
    return {
        "id": id,
        "status": "completed",
        "items": [{"id": f"i-{id}", "type": "agentMessage", "text": text}],
    }


def test_explicit_view_follows_but_background_reads_do_not():
    async def run():
        runtime, host, client = setup()
        for n in range(140):
            await runtime.get_session_state(f"s{n}", f"t{n}")
        assert not client.attached
        await runtime.prepare_session_view("s", "thread_1")
        await runtime.prepare_session_view("s", "thread_1")
        assert client.attached == {"thread_1"}
        client.thread["turns"] = [turn("one", "later output")]
        await client.refresh_state("thread_1")
        assert any(
            i.content.get("text") == "later output"
            for sync in host.timeline_syncs
            for i in sync["items"]
        )
        assert not host.turn_ends

    asyncio.run(run())


def test_snapshot_partial_keeps_checkpoint_prefix_and_complete_rollback_removes_it():
    async def run():
        runtime, host, client = setup()
        client.complete = True
        client.thread["turns"] = [turn("old", "old"), turn("new", "new")]
        prepared = await runtime.prepare_session_timeline_sync("s", "thread_1")
        await prepared.commit()
        old_ids = set(host.sync_states["codex/timeline-sync/thread_1"]["items"])
        client.complete = False
        client.thread["turns"] = [turn("new", "changed")]
        partial = await runtime.prepare_session_timeline_sync("s", "thread_1")
        assert not partial.snapshot.complete
        await partial.commit()
        assert (
            old_ids <= host.sync_states["codex/timeline-sync/thread_1"]["items"].keys()
        )
        client.complete = True
        rolled = await runtime.prepare_session_timeline_sync("s", "thread_1")
        assert rolled.snapshot.complete
        await rolled.commit()
        assert len(host.sync_states["codex/timeline-sync/thread_1"]["items"]) == 1

    asyncio.run(run())


def test_owner_loss_preserves_presentation_but_native_clear_clears_it():
    async def run():
        runtime, host, client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        client.thread["threadGoal"] = {"objective": "goal"}
        await client.refresh_state("thread_1")
        await runtime._handle_notification(event(None))
        state = await runtime.get_session_state("s", "thread_1")
        assert state.metadata["codexPresentation"]["threadGoal"] == {
            "objective": "goal"
        }
        assert state.metadata["codexCoordination"]["available"] is False
        client.thread["threadGoal"] = None
        await client.refresh_state("thread_1")
        assert (await runtime.get_session_state("s", "thread_1")).metadata[
            "codexPresentation"
        ]["threadGoal"] is None

    asyncio.run(run())


def test_notice_identity_reconnect_and_untrusted_input_cannot_retarget():
    async def run():
        runtime, host, client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        await runtime._handle_notification(event(client.thread, token="old"))
        old = (await runtime.get_session_notices("s"))[0]
        await runtime._handle_notification(event(client.thread, token="new"))
        fresh = (await runtime.get_session_notices("s"))[0]
        assert fresh.notice_id != old.notice_id
        with pytest.raises(ValueError):
            await runtime.respond_interaction("s", old.notice_id, "approve")
        await runtime.respond_interaction(
            "s",
            fresh.notice_id,
            "approve",
            {"requestId": 999, "responseContext": "evil"},
        )
        assert client.tokens == [("new", {"decision": "accept"})]

    asyncio.run(run())


def test_validation_correctable_but_unknown_dispatch_is_not_retryable():
    async def run():
        runtime, host, client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        await runtime._handle_notification(
            event(client.thread, token="question", method="item/tool/requestUserInput")
        )
        notice = (await runtime.get_session_notices("s"))[0]
        with pytest.raises(ValueError):
            await runtime.respond_interaction("s", notice.notice_id, "submit", {})
        assert runtime._notices.get(notice.notice_id).status == "open"
        assert not client.tokens
        client.response_error = TimeoutError("unknown")
        with pytest.raises(TimeoutError):
            await runtime.respond_interaction(
                "s", notice.notice_id, "submit", {"answers": {}}
            )
        failed = runtime._notices.get(notice.notice_id)
        assert failed.response_required is False
        assert failed.metadata["retryable"] is False
        with pytest.raises(ValueError):
            await runtime.respond_interaction(
                "s", notice.notice_id, "submit", {"answers": {}}
            )
        assert len(client.tokens) == 1

    asyncio.run(run())


def test_plan_artifacts_are_stable_latest_per_turn_and_preserve_markdown():
    async def run():
        runtime, host, client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        client.thread["turns"] = [
            {
                "id": "t",
                "status": "inProgress",
                "items": [
                    {
                        "id": "p1",
                        "type": "todo-list",
                        "plan": [{"step": "old", "status": "pending"}],
                    },
                    {
                        "id": "p2",
                        "type": "todo-list",
                        "explanation": "why",
                        "plan": [
                            {"step": "new", "status": "future-status", "extra": 3}
                        ],
                    },
                    {"id": "markdown", "type": "plan", "text": "# Design\nDo this"},
                ],
            }
        ]
        await client.refresh_state("thread_1")
        items = host.timeline_syncs[-1]["items"]
        progress = [i for i in items if i.content.get("kind") == "plan-progress"]
        assert len(progress) == 1
        assert progress[0].type == "artifact"
        assert progress[0].content["plan"] == [
            {"step": "new", "status": "future-status", "extra": 3}
        ]
        assert progress[0].source["itemId"] == "p2"
        assert any(
            i.content == {"kind": "plan", "text": "# Design\nDo this"} for i in items
        )
        before_id = progress[0].id
        client.thread["turns"][0]["items"].append(
            {"id": "p3", "type": "todo-list", "plan": []}
        )
        await client.refresh_state("thread_1")
        assert (
            next(
                i
                for i in host.timeline_syncs[-1]["items"]
                if i.content.get("kind") == "plan-progress"
            ).id
            == before_id
        )
        count = len(host.timeline_syncs)
        await client.refresh_state("thread_1")
        assert len(host.timeline_syncs) == count
        await runtime._handle_notification(
            {
                "method": "turn/plan/updated",
                "params": {
                    "threadId": "thread_1",
                    "turnId": "t",
                    "plan": [{"step": "live", "status": "inProgress"}],
                },
            }
        )
        live = host.timeline_item_upserts[-1]
        assert live.id == before_id and live.content["kind"] == "plan-progress"

    asyncio.run(run())


def test_view_lru_bounds_and_pins_unresolved_requests():
    async def run():
        runtime, host, client = setup()
        await runtime.prepare_session_view("s0", "t0")
        await runtime._handle_notification(
            event(client.thread, token="pending", thread_id="t0")
        )
        for n in range(1, 126):
            await runtime.prepare_session_view(f"s{n}", f"t{n}")
        assert len(client.attached) == 120
        assert "t0" in client.attached
        assert "t1" in client.detached
        assert (
            not runtime._notices.get(
                (await runtime.get_session_notices("s0"))[0].notice_id
            ).status
            == "closed"
        )

    asyncio.run(run())


def test_coordinated_owner_selections_and_authority_loss_capabilities():
    async def run():
        runtime, host, client = setup()
        client.thread.update(
            model="gpt-example",
            effort="high",
            approvalPolicy="never",
            sandboxPolicy={"type": "dangerFullAccess"},
        )
        await runtime.prepare_session_view("s", "thread_1")
        state = await runtime.get_session_state("s", "thread_1")
        assert state.selections.get("model")
        assert state.selections.get("permission")
        await runtime._handle_notification(event(None))
        caps = await runtime.get_session_capabilities("s", "thread_1")
        assert not any(
            c.available
            for c in caps.capabilities
            if c.capability_id
            in {
                "session.send_message",
                "session.steer",
                "session.interrupt",
                "session.interaction.approval",
            }
        )

    asyncio.run(run())


def test_question_ui_form_and_exact_native_answer_translation():
    async def run():
        runtime, host, client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        await runtime._handle_notification(
            event(
                client.thread,
                token="question",
                method="item/tool/requestUserInput",
                params={
                    "questions": [
                        {
                            "id": "q",
                            "question": "Pick one",
                            "header": "Choice",
                            "options": [{"label": "First", "description": "A"}],
                        }
                    ]
                },
            )
        )
        notice = (await runtime.get_session_notices("s"))[0]
        assert notice.interaction_type == "input_request"
        form = notice.actions[0]["input"]["uiSchema"]
        assert form["component"] == "inputRequest"
        option_id = form["questions"][0]["options"][0]["id"]
        await runtime.respond_interaction(
            "s",
            notice.notice_id,
            "submit",
            {"answers": {"q": {"optionIds": [option_id], "customText": "More"}}},
        )
        assert client.tokens[-1] == (
            "question",
            {"answers": {"q": {"answers": ["First", "More"]}}},
        )

    asyncio.run(run())


def test_absent_goal_is_unknown_and_cold_complete_event_replaces_persisted_history():
    async def run():
        runtime, host, client = setup()
        client.thread["threadGoal"] = {"objective": "keep"}
        await runtime.prepare_session_view("s", "thread_1")
        del client.thread["threadGoal"]
        await client.refresh_state("thread_1")
        assert (await runtime.get_session_state("s")).metadata["codexPresentation"][
            "threadGoal"
        ] == {"objective": "keep"}
        host.timeline_syncs.clear()
        runtime._notifications.coordination.published.clear()
        client.complete = True
        await client.refresh_state("thread_1")
        assert host.timeline_syncs[-1]["complete"] is True

    asyncio.run(run())


def test_control_commands_route_structured_payload_without_native_fallback():
    async def run():
        runtime, host, client = setup()
        calls = []

        async def operation(thread, method, params):
            calls.append((thread, method, params))
            return {"applied": True}

        client.owner_operation = operation
        result = await runtime.execute_command(
            "s", "update-daybreak", "thread_1", args=('{"daybreakEnabled":true}',)
        )
        assert result.ok
        assert calls == [
            ("thread_1", "thread-follower-update-daybreak", {"daybreakEnabled": True})
        ]
        with pytest.raises(ValueError):
            await runtime.execute_command(
                "s",
                "update-thread-settings",
                "thread_1",
                args=('{"conversationId":"other"}',),
            )
        assert len(calls) == 1

    asyncio.run(run())
