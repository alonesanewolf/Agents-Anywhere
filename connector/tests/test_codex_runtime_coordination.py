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
        runtime, _host, client = setup()
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
        runtime, _host, client = setup()
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
        runtime, _host, client = setup()
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
        runtime, _host, client = setup()
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
            runtime._notices.get(
                (await runtime.get_session_notices("s0"))[0].notice_id
            ).status
            != "closed"
        )

    asyncio.run(run())


def test_coordinated_owner_selections_and_authority_loss_capabilities():
    async def run():
        runtime, _host, client = setup()
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
        runtime, _host, client = setup()
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
        runtime, _host, client = setup()
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


def test_pinned_views_are_not_evicted_and_busy_mutation_is_protected():
    async def run():
        runtime, _host, client = setup()
        for n in range(120):
            await runtime.prepare_session_view(f"s{n}", f"t{n}")
            runtime._active_turn_ids[f"s{n}"] = f"turn{n}"
        with pytest.raises(RuntimeError, match="capacity"):
            await runtime.prepare_session_view("overflow", "overflow")
        assert not client.detached
        runtime._active_turn_ids.clear()
        entered, release = asyncio.Event(), asyncio.Event()

        async def operation(thread, method, payload):
            entered.set()
            await release.wait()
            return {"ok": True}

        client.owner_operation = operation
        task = asyncio.create_task(
            runtime.execute_command("s0", "compact-thread", "t0")
        )
        await entered.wait()
        await runtime.prepare_session_view("overflow", "overflow")
        assert "t0" in client.attached and client.detached == ["t1"]
        release.set()
        await task

    asyncio.run(run())


def test_canonical_latest_settings_and_token_state_are_projected():
    async def run():
        runtime, _host, client = setup()
        client.thread = {
            "id": "thread_1",
            "turns": [],
            "latestModel": "gpt-example",
            "latestReasoningEffort": "high",
            "latestThreadSettings": {"model": "gpt-example", "effort": "high"},
            "latestTokenUsageInfo": {"total": {"totalTokens": 55}},
        }
        await runtime.prepare_session_view("s", "thread_1")
        state = await runtime.get_session_state("s")
        assert state.selections.get("model")
        assert (
            state.metadata["codexSettings"]["latestTokenUsageInfo"]["total"][
                "totalTokens"
            ]
            == 55
        )

    asyncio.run(run())


def test_real_peers_receive_live_state_send_steer_interrupt_and_approval(tmp_path):
    import tempfile
    from pathlib import Path

    from test_codex_coordination_adapter import RuntimeNative

    from connector.core.json_kv import JsonKeyValueStore
    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient
    from connector.runtimes.codex.coordination.peer import CoordinationPeer
    from connector.runtimes.codex.coordination.transport import CoordinationClient
    from connector.runtimes.codex.sdk.runtime_client import CodexModelListResult

    async def run():
        with tempfile.TemporaryDirectory(prefix="aa-t4-", dir="/tmp") as directory:
            endpoint = Path(directory) / "ipc.sock"
            owner = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
            follower = CoordinationPeer(
                CoordinationClient(directory, endpoint=endpoint)
            )
            native = RuntimeNative()

            async def models():
                return CodexModelListResult(models=())

            native.list_models = models
            host = FakeHost()
            adapter = CoordinatedCodexClient(
                native,
                follower,
                kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
                namespace="integration",
            )
            runtime = CodexRuntime(
                RuntimeConfig(runtime="codex", revision=8), host, adapter
            )
            calls = []
            state = {
                "id": "remote",
                "resumeState": "resumed",
                "turns": [],
                "requests": [],
                "turnsPagination": {"hasLoadedOldest": True},
            }

            async def operation(method, params):
                calls.append((method, params))
                if method.endswith("start-turn"):
                    return {
                        "result": {"turn": {"id": "active", "status": "inProgress"}}
                    }
                if method.endswith("steer-turn"):
                    return {"result": {"turnId": "active"}}
                if method.endswith("interrupt-turn"):
                    return {"ok": True, "interruptedTurnId": "active"}
                return {"ok": True}

            owner.owner_handler = operation
            await owner.start()
            await owner.claim("remote", state)
            try:
                await runtime.prepare_session_view("s", "remote")
                assert (
                    await runtime.start_turn(
                        "s", "remote", "hello", client_message_id="m"
                    )
                ).ok
                assert (await runtime.steer_turn("s", "remote", "steer")).ok
                assert (await runtime.interrupt_session("s")).ok
                state["turns"] = [
                    {
                        "turnId": "active",
                        "status": "inProgress",
                        "items": [
                            {
                                "id": "out",
                                "type": "agentMessage",
                                "text": "owner output",
                            }
                        ],
                    }
                ]
                state["requests"] = [
                    {
                        "id": 7,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "remote", "command": "ls"},
                    }
                ]
                await owner.publish_state("remote", state)
                async with asyncio.timeout(2):
                    while not await runtime.get_session_notices("s"):
                        await asyncio.sleep(0.005)
                notice = (await runtime.get_session_notices("s"))[0]
                assert any(
                    i.content.get("text") == "owner output"
                    for sync in host.timeline_syncs
                    for i in sync["items"]
                )
                assert (
                    await runtime.respond_interaction("s", notice.notice_id, "approve")
                ).ok
                assert [method for method, _ in calls] == [
                    "thread-follower-start-turn",
                    "thread-follower-steer-turn",
                    "thread-follower-interrupt-turn",
                    "thread-follower-command-approval-decision",
                ]
                assert native.calls == []
                assert not host.turn_ends
            finally:
                await runtime.stop()
                await owner.close()

    asyncio.run(run())


def test_selection_updates_reach_owner_and_do_not_succeed_on_rejection():
    async def run():
        runtime, _host, client = setup()
        calls = []

        async def operation(thread, method, payload):
            calls.append((method, payload))
            raise ValueError("native rejected")

        client.owner_operation = operation
        await runtime.prepare_session_view("s", "thread_1")
        catalog = await runtime.list_model_catalog()
        selected = catalog.models[0].reasoning_items[0].selection_id
        with pytest.raises(ValueError, match="native rejected"):
            await runtime.update_session_selections(
                "s", "thread_1", {"model": selected}
            )
        assert calls[0][0] == "thread-follower-update-thread-settings"
        assert calls[0][1]["threadSettings"]["model"] == catalog.models[0].id
        assert (await runtime.get_session_state("s")).selections.get(
            "model"
        ) != selected

    asyncio.run(run())


def test_late_start_result_does_not_reanimate_completed_canonical_turn():
    async def run():
        runtime, _host, client = setup()
        from connector.runtimes.codex.sdk.runtime_client import CodexTurnResult

        await runtime.prepare_session_view("s", "thread_1")

        async def start(request):
            client.thread["turns"] = [turn("already-done", "done")]
            await client.refresh_state("thread_1")
            return CodexTurnResult(turn_id="already-done", payload={})

        client.start_turn = start
        await runtime.start_turn("s", "thread_1", "hello")
        assert (await runtime.get_session_state("s")).status == "idle"
        assert "s" not in runtime._active_turn_ids

    asyncio.run(run())


def test_unknown_canonical_permission_clears_stale_selection():
    async def run():
        runtime, _host, client = setup()
        client.thread.update(
            model="gpt-example",
            approvalPolicy="never",
            sandboxPolicy={"type": "dangerFullAccess"},
        )
        await runtime.prepare_session_view("s", "thread_1")
        assert (await runtime.get_session_state("s")).selections.get("permission")
        client.thread.update(
            approvalPolicy={"unknownPolicy": True}, sandboxPolicy={"type": "vendor"}
        )
        await client.refresh_state("thread_1")
        assert (await runtime.get_session_state("s")).selections.get(
            "permission"
        ) is None

    asyncio.run(run())


def test_partial_items_do_not_overwrite_an_earlier_physical_message():
    async def run():
        runtime, host, client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        first = {"id": "a", "type": "agentMessage", "text": "first"}
        second = {"id": "b", "type": "agentMessage", "text": "second"}
        client.thread["turns"] = [
            {"id": "t", "status": "completed", "items": [first, second]}
        ]
        await client.refresh_state("thread_1")
        initial = {
            i.source.get("itemId"): i.id for i in host.timeline_syncs[-1]["items"]
        }
        client.thread["turns"][0]["items"] = [{**second, "text": "second changed"}]
        client.thread["turns"][0]["itemsPagination"] = {"hasLoadedOldest": False}
        await client.refresh_state("thread_1")
        item = host.timeline_syncs[-1]["items"][0]
        assert item.id == initial["b"] and item.id != initial["a"]
        assert not host.timeline_syncs[-1]["complete"]

    asyncio.run(run())


def test_scoped_cleanup_does_not_make_cold_thread_unstartable():
    async def run():
        runtime, _host, _client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        cleanup = event(None)
        cleanup["params"]["coordination"]["role"] = "unattached"
        await runtime._handle_notification(cleanup)
        assert (await runtime.start_turn("s", "thread_1", "hello")).ok

    asyncio.run(run())


def test_unknown_response_pins_follow_until_native_request_resolves():
    async def run():
        runtime, _host, client = setup()
        await runtime.prepare_session_view("s0", "t0")
        message = event(client.thread, token="uncertain", thread_id="t0")
        await runtime._handle_notification(message)
        notice = (await runtime.get_session_notices("s0"))[0]
        client.response_error = TimeoutError("unknown")
        with pytest.raises(TimeoutError):
            await runtime.respond_interaction("s0", notice.notice_id, "approve")
        message["params"][
            "requestContexts"
        ] = []  # Token consumed, request still native-pending.
        await runtime._handle_notification(message)
        for n in range(1, 123):
            await runtime.prepare_session_view(f"s{n}", f"t{n}")
        assert "t0" in client.attached
        message["params"]["requests"] = []
        await runtime._handle_notification(message)
        await runtime.prepare_session_view("next", "next")
        assert "t0" in client.detached

    asyncio.run(run())


@pytest.mark.parametrize(
    "method,action,data,expected",
    [
        ("item/fileChange/requestApproval", "reject", {}, {"decision": "decline"}),
        (
            "item/permissions/requestApproval",
            "approve",
            {
                "permissions": {"network": {"enabled": True}},
                "approvalSource": {"method": "evil"},
            },
            {"scope": "turn", "permissions": {"fileSystem": {"read": ["/safe"]}}},
        ),
        (
            "mcpServer/elicitation/request",
            "accept",
            {"content": {"value": "ok"}, "_meta": {"future": 1}},
            {"action": "accept", "content": {"value": "ok"}, "_meta": {"future": 1}},
        ),
    ],
)
def test_interactions_preserve_exact_payload_and_trusted_permissions(
    method, action, data, expected
):
    async def run():
        runtime, _host, client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        await runtime._handle_notification(
            event(
                client.thread,
                token="token",
                method=method,
                params={
                    "mode": "form",
                    "permissions": {"fileSystem": {"read": ["/safe"]}},
                },
            )
        )
        notice = (await runtime.get_session_notices("s"))[0]
        await runtime.respond_interaction("s", notice.notice_id, action, data)
        assert client.tokens == [("token", expected)]

    asyncio.run(run())


def test_mcp_form_uses_generic_input_and_special_accept_stays_correctable():
    async def run():
        runtime, _host, client = setup()
        await runtime.prepare_session_view("s", "thread_1")
        await runtime._handle_notification(
            event(
                client.thread,
                token="mcp",
                method="mcpServer/elicitation/request",
                params={
                    "mode": "form",
                    "requestedSchema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                },
            )
        )
        notice = (await runtime.get_session_notices("s"))[0]
        assert notice.interaction_type == "input_request"
        accept = notice.actions[0]
        assert accept["input"]["uiSchema"]["component"] == "inputRequest"
        with pytest.raises(ValueError):
            await runtime.respond_interaction(
                "s", notice.notice_id, "accept", {"content": {}}
            )
        assert runtime._notices.get(notice.notice_id).status == "open"
        await runtime.respond_interaction(
            "s",
            notice.notice_id,
            "accept",
            {"answers": {"content": {"optionIds": [], "customText": '{"name":"Ada"}'}}},
        )
        assert client.tokens[-1] == (
            "mcp",
            {"action": "accept", "content": {"name": "Ada"}},
        )
        await runtime._handle_notification(
            event(
                client.thread,
                token="special",
                method="mcpServer/elicitation/request",
                params={"mode": "vendor/auth"},
            )
        )
        special = (await runtime.get_session_notices("s"))[0]
        with pytest.raises(ValueError):
            await runtime.respond_interaction("s", special.notice_id, "accept")
        assert runtime._notices.get(special.notice_id).metadata["retryable"] is True
        await runtime.respond_interaction("s", special.notice_id, "decline")

    asyncio.run(run())


def test_explicit_read_rpc_calls_optional_view_hook_only():
    from connector.runtime_protocol import AgentRuntime, RuntimeIdentity
    from connector.server.runtime_session_rpc import (
        read_session_notices,
        read_session_state,
        sync_session_snapshot,
    )

    class Runtime(AgentRuntime):
        def __init__(self):
            self.views = []

        @property
        def identity(self):
            return RuntimeIdentity(runtime="test")

        async def prepare_session_view(self, session_id, external_session_id=None):
            self.views.append((session_id, external_session_id))

        async def get_session_snapshot(
            self, session_id, external_session_id=None, limit=None
        ):
            from connector.runtime_protocol import RuntimeTimelineSnapshot

            return RuntimeTimelineSnapshot(
                session_id=session_id,
                external_session_id=external_session_id,
                runtime="test",
                items=(),
            )

    async def run():
        runtime, host = Runtime(), FakeHost()
        params = {"sessionId": "s", "externalSessionId": "t"}
        await runtime.get_session_state("s", "t")
        assert runtime.views == []
        await read_session_state(runtime, host, params)
        await read_session_notices(runtime, params)
        await sync_session_snapshot(runtime, host, params)
        assert runtime.views == [("s", "t")] * 3

    asyncio.run(run())


def test_sdk_typed_markdown_plan_deltas_share_snapshot_identity():
    from openai_codex.generated.v2_all import (
        PlanDeltaNotification,
        PlanThreadItem,
        ThreadItem,
    )

    from connector.runtimes.codex.sdk.events import CodexSdkEvent
    from connector.runtimes.codex.timeline.accumulator import CodexTimelineAccumulator
    from connector.runtimes.codex.timeline.typed_events import (
        timeline_projection_from_thread_item,
    )

    timeline = CodexTimelineAccumulator()
    for text in ("# Plan", "\nStep one"):
        payload = PlanDeltaNotification(
            thread_id="thread_1", turn_id="t", item_id="p", delta=text
        )
        event_value = CodexSdkEvent.from_parts(
            event_type="item/plan/delta",
            params={"threadId": "thread_1", "turnId": "t", "itemId": "p"},
            raw={},
            payload=payload,
            legacy_method_shaped=False,
        )
        live = timeline.item_from_event("s", "thread_1", event_value)
    assert live.type == "artifact" and live.content == {
        "kind": "plan",
        "text": "# Plan\nStep one",
    }
    projection = timeline_projection_from_thread_item(
        ThreadItem(PlanThreadItem(id="p", text="# Plan\nStep one", type="plan")),
        "t",
        "completed",
    )
    snapshot = timeline.items_from_snapshot_projections("s", "thread_1", (projection,))
    assert snapshot[0].id == live.id and snapshot[0].content == live.content


def test_passive_state_returns_owner_activity_without_persistent_attach():
    async def run():
        runtime, _host, client = setup()
        client.thread["turns"] = [{"id": "active", "status": "inProgress", "items": []}]
        state = await runtime.get_session_state("cold", "thread_1")
        assert state.status == "running"
        assert client.attached == set()

    asyncio.run(run())


def test_plan_order_and_snapshot_limit_follow_physical_turns():
    async def run():
        runtime, _host, client = setup()
        client.thread["turns"] = [
            {
                "id": "t1",
                "status": "completed",
                "items": [{"id": "p", "type": "plan", "text": "plan"}],
            },
            turn("t2", "later"),
        ]
        snapshot = await runtime.get_session_snapshot("s", "thread_1")
        assert [item.turn_id for item in snapshot.items] == ["t1", "t2"]
        assert [item.order_seq for item in snapshot.items] == [0, 1]
        snapshot = await runtime.get_session_snapshot("s", "thread_1", limit=1)
        assert len(snapshot.items) == 1 and snapshot.items[0].turn_id == "t2"

    asyncio.run(run())


def test_explicit_unowned_view_hydrates_native_state_without_claim():
    async def run():
        runtime, _host, client = setup()

        async def attach(thread_id):
            return None

        client.attach_thread = attach

        async def refresh(thread_id, **kwargs):
            message = event(None)
            message["params"]["coordination"]["role"] = "unattached"
            await client.handler(message)

        client.refresh_state = refresh
        client.thread["turns"] = [{"id": "busy", "status": "inProgress", "items": []}]
        await runtime.prepare_session_view("s", "thread_1")
        assert (await runtime.get_session_state("s")).status == "running"
        assert client.attached == set()

    asyncio.run(run())


def test_history_control_publishes_full_canonical_history_and_owner_metadata():
    from connector.runtimes.codex.coordination.projection import native_to_state

    async def run():
        runtime, host, client = setup()

        async def operation(thread, method, payload):
            return {}

        async def history(thread):
            return native_to_state(
                {"id": thread, "turns": [turn("old", "old")]}, complete=True
            )

        client.owner_operation = operation
        client.load_complete_history = history
        await runtime.execute_command("s", "load-complete-history", "thread_1")
        assert host.timeline_syncs[-1]["complete"] is True
        assert host.timeline_syncs[-1]["items"][0].content["text"] == "old"
        await runtime.prepare_session_view("s", "thread_1")
        client.thread.update(title="Remote title", cwd="/remote")
        await client.refresh_state("thread_1")
        assert host.meta_upserts[-1]["title"] == "Remote title"
        assert host.meta_upserts[-1]["cwd"] == "/remote"

    asyncio.run(run())
