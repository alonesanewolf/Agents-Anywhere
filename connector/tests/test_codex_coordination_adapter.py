import tempfile
from pathlib import Path

import pytest
from test_codex_coordination_operations import Native, async_test

from connector.core.json_kv import JsonKeyValueStore
from connector.runtimes.codex.coordination.peer import CoordinationPeer
from connector.runtimes.codex.coordination.transport import CoordinationClient
from connector.runtimes.codex.sdk.runtime_client import CodexStartTurnRequest


class RuntimeNative(Native):
    def set_native_event_handler(self, handler):
        self.handler = handler

    async def start(self, handler):
        self.normalized = handler

    async def stop(self):
        pass

    async def native_thread_resume(self, thread_id):
        return await self.native_request("thread/resume", {"threadId": thread_id})


@async_test
async def test_passive_cold_read_never_claims_and_remote_send_never_mutates_local(
    tmp_path,
):
    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient

    with tempfile.TemporaryDirectory(prefix="aa-coord-", dir="/tmp") as directory:
        endpoint = Path(directory) / "ipc.sock"
        owner = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        follower = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        native = RuntimeNative()
        native.responses["thread/read"] = {"thread": {"id": "cold", "turns": []}}
        events, forwarded = [], []

        async def event(value):
            events.append(value)

        async def operation(method, params):
            forwarded.append((method, params))
            return {"result": {"turn": {"id": "remote-turn", "status": "inProgress"}}}

        adapter = CoordinatedCodexClient(
            native,
            follower,
            kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
            namespace="runtime",
        )
        await owner.start()
        await adapter.start(event)
        try:
            result = await adapter.read_thread("cold")
            assert result.thread["id"] == "cold"
            assert not follower.is_owner("cold")
            assert [call[0] for call in native.calls] == ["thread/read"]
            owner.owner_handler = operation
            await owner.claim(
                "remote",
                {
                    "id": "remote",
                    "resumeState": "resumed",
                    "turns": [],
                    "turnsPagination": {"hasLoadedOldest": True},
                    "requests": [],
                    "future": {"x": 1},
                },
            )
            await adapter.attach_thread("remote")
            read = await adapter.read_thread("remote")
            assert read.thread["future"] == {"x": 1}
            started = await adapter.start_turn(
                CodexStartTurnRequest(thread_id="remote", content="hi")
            )
            assert started.turn_id == "remote-turn"
            assert (
                forwarded[0][1]["turnStart"]["context"]["inheritThreadSettings"] is True
            )
            await native.handler(
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": "remote", "goal": {"status": "wrong"}},
                },
                1,
            )
            assert follower.get_state("remote").get("threadGoal") is None
            assert [call[0] for call in native.calls] == ["thread/read"]
        finally:
            await adapter.stop()
            await owner.close()


@async_test
async def test_aa_owner_raw_state_and_response_context_reject_reused_id(tmp_path):
    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient

    with tempfile.TemporaryDirectory(prefix="aa-coord-", dir="/tmp") as directory:
        endpoint = Path(directory) / "ipc.sock"
        peer = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        native = RuntimeNative()
        native.responses["thread/resume"] = {"thread": {"id": "t", "turns": []}}
        native.responses["turn/start"] = {
            "turn": {"id": "physical", "status": "inProgress", "items": []}
        }
        events = []

        async def event(value):
            events.append(value)

        adapter = CoordinatedCodexClient(
            native,
            peer,
            kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
            namespace="runtime",
        )
        await adapter.start(event)
        try:
            result = await adapter.start_turn(
                CodexStartTurnRequest(thread_id="t", content="start")
            )
            assert result.turn_id == "physical"
            request = {
                "id": 7,
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "t", "questions": []},
            }
            await native.handler(request, 1)
            await adapter.refresh_state("t")
            shown = events[-1]["params"]
            token = shown["requestContexts"][0]["responseContext"]
            await adapter.refresh_state("t", force=True)
            assert (
                events[-1]["params"]["requestContexts"][0]["responseContext"] == token
            )
            await adapter.respond_to_request(token, {"answers": {}})
            assert native.calls[-1][0] == "respond"
            await native.handler(request, 1)
            await adapter.refresh_state("t", force=True)
            fresh = events[-1]["params"]["requestContexts"][0]["responseContext"]
            assert fresh != token
            with pytest.raises(ValueError, match="stale"):
                await adapter.respond_to_request(token, {"answers": {}})
            native.native_generation = 2
            with pytest.raises(ValueError, match="stale"):
                await adapter.respond_to_request(fresh, {"answers": {}})
        finally:
            await adapter.stop()


@async_test
async def test_all_fourteen_follower_routes_execute_against_aa_owner_over_real_peers(
    tmp_path,
):
    from connector.runtimes.codex.coordination.journal import CoordinationJournal
    from connector.runtimes.codex.coordination.operations import OwnerOperations
    from connector.runtimes.codex.coordination.projection import native_to_state

    with tempfile.TemporaryDirectory(prefix="aa-coord-", dir="/tmp") as directory:
        endpoint = Path(directory) / "ipc.sock"
        owner = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        follower = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        native = Native()
        native.responses.update(
            {
                "turn/start": {
                    "turn": {"id": "turn", "status": "inProgress", "items": []}
                },
                "turn/steer": {"turnId": "turn"},
                "thread/metadata/update": {
                    "thread": {"id": "t", "daybreakEnabled": True}
                },
                "thread/read": {"thread": {"id": "t", "turns": []}},
                "thread/turns/list": {"data": [], "nextCursor": None},
                "thread/rollback": {"thread": {"id": "t", "turns": []}},
            }
        )
        journal = CoordinationJournal(
            JsonKeyValueStore(tmp_path / "kv.json"), "runtime"
        )
        operations = OwnerOperations(native, owner, journal)
        owner.owner_handler = operations.handle
        await owner.start()
        await follower.start()
        try:
            await owner.claim(
                "t", native_to_state({"id": "t", "turns": []}, complete=True)
            )
            await follower.follow("t")
            count = 0

            async def route(suffix, params):
                nonlocal count
                count += 1
                return await follower.request_owner(
                    "t", "thread-follower-" + suffix, {"conversationId": "t", **params}
                )

            assert await route(
                "start-turn",
                {
                    "turnStart": {
                        "request": {
                            "threadId": "t",
                            "input": [{"type": "text", "text": "hi", "unknown": 5}],
                        }
                    }
                },
            ) == {
                "result": {"turn": {"id": "turn", "status": "inProgress", "items": []}}
            }
            assert await route(
                "steer-turn",
                {
                    "input": [{"type": "text", "text": "steer"}],
                    "restoreMessage": {"cwd": "/workspace", "future": 1},
                },
            ) == {"result": {"turnId": "turn"}}
            assert await route(
                "interrupt-turn", {"expectedTurnId": "turn", "mode": "system"}
            ) == {"ok": True, "interruptedTurnId": "turn"}
            assert await route("compact-thread", {}) == {"ok": True}
            assert await route(
                "update-thread-settings", {"threadSettings": {"effort": "high"}}
            ) == {"applied": True}
            assert await route("update-daybreak", {"daybreakEnabled": True}) == {
                "ok": True
            }
            history = await route("load-complete-history", {})
            observed = await follower.wait_revision("t", history["revision"])
            assert observed["turnsPagination"]["hasLoadedOldest"] is True
            await owner.publish_state(
                "t",
                native_to_state(
                    {
                        "id": "t",
                        "turns": [
                            {
                                "id": "old",
                                "status": "completed",
                                "items": [
                                    {
                                        "id": "u",
                                        "type": "userMessage",
                                        "content": [{"type": "text", "text": "old"}],
                                    }
                                ],
                            }
                        ],
                    },
                    complete=True,
                ),
            )
            assert await route(
                "edit-last-user-turn", {"turnId": "old", "message": "edited"}
            ) == {"ok": True}
            cases = [
                (
                    "command-approval-decision",
                    "item/commandExecution/requestApproval",
                    {
                        "decision": {
                            "acceptWithExecpolicyAmendment": {
                                "execpolicy_amendment": ["ls"]
                            }
                        }
                    },
                ),
                (
                    "file-approval-decision",
                    "item/fileChange/requestApproval",
                    {"decision": "decline"},
                ),
                (
                    "permissions-request-approval-response",
                    "item/permissions/requestApproval",
                    {"response": {"permissions": {}, "scope": "turn"}},
                ),
                (
                    "submit-user-input",
                    "item/tool/requestUserInput",
                    {"response": {"answers": {"q": {"answers": ["a"]}}}},
                ),
                (
                    "submit-mcp-server-elicitation-response",
                    "mcpServer/elicitation/request",
                    {
                        "response": {
                            "action": "accept",
                            "content": {"value": "ok"},
                            "_meta": {"future": True},
                        }
                    },
                ),
            ]
            for index, (suffix, method, params) in enumerate(cases):
                state = owner.get_state("t")
                state["requests"] = [
                    {
                        "id": index,
                        "method": method,
                        "params": {"threadId": "t", "mode": "form", "questions": []},
                    }
                ]
                await owner.publish_state("t", state)
                assert await route(suffix, {"requestId": index, **params}) == {
                    "ok": True
                }
                assert native.calls[-1][0] == "respond"
                assert native.calls[-1][1]["id"] == index
            assert await route(
                "set-queued-follow-ups-state",
                {"state": {"t": [{"id": "queued", "text": "later"}]}},
            ) == {"ok": True}
            assert journal.queue("t")[0]["text"] == "later"
            assert count == 14
        finally:
            await follower.close()
            await owner.close()


@async_test
async def test_normal_aa_approval_setting_is_translated_and_follower_goal_never_resumes(
    tmp_path,
):
    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient

    with tempfile.TemporaryDirectory(prefix="aa-coord-", dir="/tmp") as directory:
        endpoint = Path(directory) / "ipc.sock"
        peer = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        native = RuntimeNative()
        native.responses["thread/resume"] = {"thread": {"id": "t", "turns": []}}
        native.responses["turn/start"] = {
            "turn": {"id": "physical", "status": "inProgress", "items": []}
        }
        adapter = CoordinatedCodexClient(
            native,
            peer,
            kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
            namespace="runtime",
        )

        async def event(value):
            pass

        await adapter.start(event)
        try:
            await adapter.start_turn(
                CodexStartTurnRequest(
                    thread_id="t", content="hi", approval_policy="request_approval"
                )
            )
            assert native.calls[-1][1]["approvalPolicy"] == "on-request"
            with pytest.raises(ValueError, match="owned"):
                await adapter.native_request(
                    "thread/goal/set",
                    {"threadId": "someone-elses-thread", "objective": "no"},
                )
            assert len(native.calls) == 2
        finally:
            await adapter.stop()


@async_test
async def test_cold_complete_history_uses_native_pages_without_acquiring_owner(
    tmp_path,
):
    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient

    with tempfile.TemporaryDirectory(prefix="aa-coord-", dir="/tmp") as directory:
        peer = CoordinationPeer(
            CoordinationClient(directory, endpoint=Path(directory) / "ipc.sock")
        )
        native = RuntimeNative()
        native.responses["thread/read"] = {
            "thread": {"id": "cold", "historyMode": "paginated", "turns": []}
        }
        native.responses["thread/turns/list"] = {
            "data": [
                {
                    "id": "old",
                    "status": "completed",
                    "items": [
                        {"id": "a", "type": "agentMessage", "text": "all history"}
                    ],
                }
            ],
            "nextCursor": None,
        }
        adapter = CoordinatedCodexClient(
            native,
            peer,
            kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
            namespace="runtime",
        )

        async def event(value):
            pass

        await adapter.start(event)
        try:
            result = await adapter.list_thread_turns("cold")
            assert result.turns[0]["id"] == "old"
            assert not peer.is_owner("cold")
            assert not peer.is_follower("cold")
            assert [call[0] for call in native.calls] == [
                "thread/read",
                "thread/turns/list",
            ]
        finally:
            await adapter.stop()


@async_test
async def test_queue_wakeup_during_running_send_is_not_lost(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from connector.runtimes.codex.coordination import client as module

    runs, entered, release = [], asyncio.Event(), asyncio.Event()

    async def execute(operations, thread_id):
        runs.append(thread_id)
        if len(runs) == 1:
            entered.set()
            await release.wait()
        return False

    monkeypatch.setattr(module, "execute_head", execute)
    adapter = module.CoordinatedCodexClient(
        RuntimeNative(),
        SimpleNamespace(is_owner=lambda thread_id: True),
        kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
        namespace="runtime",
    )
    await adapter.journal.replace_queue("t", [{"id": "head", "text": "queued"}])
    adapter._kick_queue("t")
    await entered.wait()
    adapter._kick_queue("t")
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert runs == ["t", "t"]


@async_test
async def test_confirmed_queue_recovery_advances_next_head_without_external_wake(
    tmp_path,
):
    import asyncio

    from test_codex_coordination_operations import Owner

    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient

    peer = Owner(
        [
            {
                "id": "done",
                "status": "completed",
                "items": [
                    {"id": "answer", "type": "agentMessage", "text": "done"},
                ],
            }
        ]
    )
    native = RuntimeNative()
    native.responses["turn/start"] = {"turn": {"id": "two", "status": "inProgress"}}
    adapter = CoordinatedCodexClient(
        native,
        peer,
        kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
        namespace="runtime",
    )
    await adapter.journal.begin("t", {"clientUserMessageId": "one"})
    await adapter.journal.stage("t", "confirmed", result={"turn": {"id": "done"}})
    await adapter.journal.replace_queue(
        "t",
        [
            {"id": "one", "text": "already sent"},
            {"id": "two", "text": "ready"},
            {"id": "three", "text": "must wait for active turn"},
        ],
    )
    adapter._kick_queue("t")
    async with asyncio.timeout(2):
        while adapter.queue_tasks:
            await asyncio.gather(*adapter.queue_tasks.values())
            await asyncio.sleep(0)
    assert [message["id"] for message in adapter.journal.queue("t")] == ["three"]
    assert len(native.calls) == 1
    assert native.calls[0][1]["clientUserMessageId"] == "two"
    assert not adapter.queue_tasks
