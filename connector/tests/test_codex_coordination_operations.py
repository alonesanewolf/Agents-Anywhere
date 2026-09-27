import asyncio
from copy import deepcopy
from functools import wraps

import pytest

from connector.core.json_kv import JsonKeyValueStore
from connector.runtimes.codex.coordination.journal import CoordinationJournal
from connector.runtimes.codex.coordination.projection import native_to_state


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class Native:
    native_generation = 1

    def __init__(self):
        self.calls = []
        self.responses = {}

    def native_runtime_info(self):
        return {"version": "0.155.1", "generation": 1, "rawEvents": True}

    async def native_request(self, method, params):
        self.calls.append((method, deepcopy(params)))
        result = self.responses.get(method, {})
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return await result(params)
        return deepcopy(result)

    async def respond_native_request(self, request_id, result, **context):
        self.calls.append(
            ("respond", {"id": request_id, "result": deepcopy(result), **context})
        )


class Owner:
    host_id = "local"

    def __init__(self, turns=()):
        self.state = native_to_state({"id": "t", "turns": list(turns)}, complete=True)
        self.owned = True
        self.revision = 1
        self.events = []

    def is_owner(self, thread_id):
        return self.owned and thread_id == "t"

    def get_state(self, thread_id):
        return deepcopy(self.state)

    async def publish_state(self, thread_id, state):
        self.state = deepcopy(state)
        self.revision += 1
        return self.revision

    async def publish_event(self, method, params):
        self.events.append((method, deepcopy(params)))


def setup(tmp_path, turns=()):
    from connector.runtimes.codex.coordination.operations import OwnerOperations

    native, peer = Native(), Owner(turns)
    journal = CoordinationJournal(JsonKeyValueStore(tmp_path / "kv.json"), "runtime")
    return OwnerOperations(native, peer, journal), native, peer, journal


@async_test
async def test_start_injects_once_preserves_typed_input_and_journals_unknown(tmp_path):
    operations, native, _peer, journal = setup(tmp_path)
    native.responses["turn/start"] = TimeoutError("unknown outcome")
    params = {
        "conversationId": "t",
        "turnStart": {
            "request": {
                "threadId": "t",
                "input": [{"type": "text", "text": "hello", "future": 1}],
                "clientUserMessageId": "m",
            },
            "context": {
                "responseItems": [{"type": "message", "role": "user", "content": []}],
                "inheritThreadSettings": True,
            },
        },
    }
    with pytest.raises(TimeoutError):
        await operations.handle("thread-follower-start-turn", params)
    assert [call[0] for call in native.calls] == ["thread/inject_items", "turn/start"]
    assert native.calls[1][1]["input"][0]["future"] == 1
    assert journal.operation("t")["stage"] == "unknown"
    with pytest.raises(ValueError, match="unconfirmed"):
        await operations.handle("thread-follower-start-turn", params)
    assert len(native.calls) == 2


@async_test
async def test_stale_interrupt_and_serialized_conditional_settings(tmp_path):
    operations, native, peer, _journal = setup(
        tmp_path, [{"id": "new", "status": "inProgress", "items": []}]
    )
    result = await operations.handle(
        "thread-follower-interrupt-turn",
        {"conversationId": "t", "expectedTurnId": "old", "mode": "system"},
    )
    assert result == {"ok": True, "interruptedTurnId": None}
    assert native.calls == []
    peer.state["latestReasoningEffort"] = "high"
    peer.state["latestModel"] = "model"
    assert await operations.handle(
        "thread-follower-update-thread-settings",
        {
            "conversationId": "t",
            "threadSettings": {"effort": "max"},
            "condition": {"ifEffortEquals": "low"},
        },
    ) == {"applied": False}
    assert native.calls == []
    assert await operations.handle(
        "thread-follower-update-thread-settings",
        {
            "conversationId": "t",
            "threadSettings": {"effort": "max"},
            "condition": {"ifEffortEquals": "high", "ifModelEquals": "model"},
        },
    ) == {"applied": True}
    assert peer.state["latestReasoningEffort"] == "max"
    assert native.calls == [
        ("thread/settings/update", {"threadId": "t", "effort": "max"})
    ]


@async_test
async def test_approvals_validate_exact_native_type_and_outstanding_method(tmp_path):
    operations, native, peer, _journal = setup(tmp_path)
    peer.state["requests"] = [
        {
            "id": 7,
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "t", "questions": []},
        }
    ]
    for suffix, request_id in [
        ("command-approval-decision", 7),
        ("submit-user-input", "7"),
    ]:
        with pytest.raises(ValueError, match="outstanding"):
            await operations.handle(
                "thread-follower-" + suffix,
                {
                    "conversationId": "t",
                    "requestId": request_id,
                    "response": {"answers": {}},
                },
            )
    response = {"answers": {"q": {"answers": ["choice"]}}, "extra": True}
    assert await operations.handle(
        "thread-follower-submit-user-input",
        {"conversationId": "t", "requestId": 7, "response": response},
    ) == {"ok": True}
    assert native.calls[0][1]["id"] == 7
    assert native.calls[0][1]["result"] == response
    assert peer.state["requests"] == []
    with pytest.raises(ValueError):
        await operations.handle(
            "thread-follower-submit-user-input",
            {"conversationId": "t", "requestId": 7, "response": response},
        )


@async_test
async def test_edit_rejects_unsupported_context_before_rollback_and_restart_failure_truthful(
    tmp_path,
):
    operations, native, peer, journal = setup(
        tmp_path,
        [
            {
                "id": "u",
                "status": "completed",
                "items": [
                    {
                        "id": "item",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "original"}],
                    }
                ],
            }
        ],
    )
    with pytest.raises(ValueError, match="permission"):
        await operations.handle(
            "thread-follower-edit-last-user-turn",
            {
                "conversationId": "t",
                "turnId": "u",
                "message": "edited",
                "shouldSendPermissionOverrides": True,
            },
        )
    assert native.calls == []
    native.responses["thread/rollback"] = {"thread": {"id": "t", "turns": []}}
    native.responses["turn/start"] = RuntimeError("restart failed")
    with pytest.raises(RuntimeError, match="restart failed"):
        await operations.handle(
            "thread-follower-edit-last-user-turn",
            {"conversationId": "t", "turnId": "u", "message": "edited"},
        )
    assert [call[0] for call in native.calls] == ["thread/rollback", "turn/start"]
    assert peer.state["turns"] == []
    assert journal.operation("t")["stage"] == "unknown"


@async_test
async def test_full_history_hydrates_items_before_publishing_revision(tmp_path):
    operations, native, peer, _journal = setup(tmp_path)
    native.responses["thread/read"] = {"thread": {"id": "t", "turns": []}}
    native.responses["thread/turns/list"] = {
        "data": [
            {
                "id": "u",
                "status": "completed",
                "items": [],
                "itemsPagination": {"hasLoadedOldest": False},
            }
        ],
        "nextCursor": None,
    }
    native.responses["thread/items/list"] = {
        "data": [
            {
                "turnId": "u",
                "item": {
                    "id": "a",
                    "type": "agentMessage",
                    "text": "history",
                    "future": 5,
                },
            }
        ],
        "nextCursor": None,
    }
    result = await operations.handle(
        "thread-follower-load-complete-history", {"conversationId": "t"}
    )
    assert result["revision"] == peer.revision
    assert peer.state["turns"][0]["items"][0]["future"] == 5
    assert peer.state["turns"][0]["itemsPagination"]["hasLoadedOldest"] is True


@async_test
async def test_daybreak_requires_native_echo_and_queue_persists_full_record(tmp_path):
    operations, native, peer, journal = setup(tmp_path)
    native.responses["thread/metadata/update"] = {"thread": {"id": "t"}}
    with pytest.raises(ValueError, match="daybreak"):
        await operations.handle(
            "thread-follower-update-daybreak",
            {"conversationId": "t", "daybreakEnabled": True},
        )
    native.responses["thread/metadata/update"] = {
        "thread": {"id": "t", "daybreakEnabled": True}
    }
    assert await operations.handle(
        "thread-follower-update-daybreak",
        {"conversationId": "t", "daybreakEnabled": True},
    ) == {"ok": True}
    record = {
        "id": "q",
        "text": "hi",
        "context": {"prompt": "hi", "future": {"a": 1}},
        "future": 2,
    }
    assert await operations.handle(
        "thread-follower-set-queued-follow-ups-state",
        {"conversationId": "t", "state": {"t": [record], "other": ["ignored"]}},
    ) == {"ok": True}
    assert journal.queue("t")[0]["future"] == 2
    assert peer.events[-1][1]["messages"][0]["context"]["future"] == {"a": 1}


@async_test
async def test_publication_rechecks_owner_after_native_response(tmp_path):
    operations, native, peer, _journal = setup(tmp_path)

    async def changed_owner(params):
        peer.owned = False
        return {"turn": {"id": "sent", "status": "inProgress", "items": []}}

    native.responses["turn/start"] = changed_owner
    with pytest.raises(ValueError, match="owner"):
        await operations.handle(
            "thread-follower-start-turn",
            {
                "conversationId": "t",
                "turnStart": {"request": {"threadId": "t", "input": []}},
            },
        )
    assert _journal.operation("t")["stage"] == "unknown"


@async_test
async def test_observed_client_message_reconciles_unknown_start_but_not_unknown_injection(
    tmp_path,
):
    operations, _native, peer, journal = setup(tmp_path)
    await journal.begin("t", {"clientUserMessageId": "m", "input": []})
    await journal.stage("t", "unknown", uncertainMethod="turn/start")
    peer.state["turns"] = [
        {
            "turnId": "physical",
            "status": "inProgress",
            "params": {"clientUserMessageId": "m"},
            "items": [],
        }
    ]
    assert await operations.reconcile("t") is True
    assert journal.operation("t")["result"]["turn"]["id"] == "physical"
    await journal.begin("t", {"clientUserMessageId": "m", "input": []})
    await journal.stage("t", "unknown", uncertainMethod="thread/inject_items")
    assert await operations.reconcile("t") is False


@async_test
async def test_persistence_failure_after_confirmed_native_start_never_sends_again(
    tmp_path,
):
    operations, native, _peer, journal = setup(tmp_path)
    native.responses["turn/start"] = {
        "turn": {"id": "physical", "status": "inProgress", "items": []}
    }
    original = journal._persist

    def broken_confirmation(document):
        if document["operations"].get("t", {}).get("stage") == "confirmed":
            raise OSError("disk full")
        original(document)

    journal._persist = broken_confirmation
    params = {
        "conversationId": "t",
        "turnStart": {"request": {"threadId": "t", "input": []}},
    }
    with pytest.raises(OSError):
        await operations.handle("thread-follower-start-turn", params)
    with pytest.raises(OSError):
        await operations.handle("thread-follower-start-turn", params)
    assert len(native.calls) == 1


@async_test
async def test_mcp_special_verification_is_rejected_before_native_response(tmp_path):
    operations, native, peer, _journal = setup(tmp_path)
    peer.state["requests"] = [
        {
            "id": "r",
            "method": "mcpServer/elicitation/request",
            "params": {"threadId": "t", "mode": "vendor/auth"},
        }
    ]
    with pytest.raises(ValueError, match="verification"):
        await operations.handle(
            "thread-follower-submit-mcp-server-elicitation-response",
            {"conversationId": "t", "requestId": "r", "response": {"action": "accept"}},
        )
    assert native.calls == []


@async_test
async def test_settings_condition_distinguishes_missing_effort_from_explicit_null(
    tmp_path,
):
    operations, native, peer, _journal = setup(tmp_path)
    peer.state["latestReasoningEffort"] = None
    result = await operations.handle(
        "thread-follower-update-thread-settings",
        {"conversationId": "t", "threadSettings": {"model": "next"}, "condition": {}},
    )
    assert result == {"applied": False}
    assert native.calls == []
    result = await operations.handle(
        "thread-follower-update-thread-settings",
        {
            "conversationId": "t",
            "threadSettings": {"model": "next"},
            "condition": {"ifEffortEquals": None},
        },
    )
    assert result == {"applied": True}


@async_test
async def test_settings_cannot_override_owned_thread_identity(tmp_path):
    operations, native, _peer, _journal = setup(tmp_path)
    with pytest.raises(ValueError, match="threadId"):
        await operations.handle(
            "thread-follower-update-thread-settings",
            {
                "conversationId": "t",
                "threadSettings": {"threadId": "foreign", "model": "next"},
            },
        )
    assert native.calls == []


@async_test
async def test_start_waits_for_settings_and_inherits_confirmed_values(tmp_path):
    operations, native, _peer, _journal = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def settings(params):
        entered.set()
        await release.wait()
        return {}

    native.responses["thread/settings/update"] = settings
    native.responses["turn/start"] = {
        "turn": {"id": "physical", "status": "inProgress", "items": []}
    }
    setting = asyncio.create_task(
        operations.handle(
            "thread-follower-update-thread-settings",
            {"conversationId": "t", "threadSettings": {"model": "new-model"}},
        )
    )
    await entered.wait()
    starting = asyncio.create_task(
        operations.handle(
            "thread-follower-start-turn",
            {
                "conversationId": "t",
                "turnStart": {
                    "request": {"threadId": "t", "input": []},
                    "context": {"inheritThreadSettings": True},
                },
            },
        )
    )
    await asyncio.sleep(0)
    assert len(native.calls) == 1
    release.set()
    await asyncio.gather(setting, starting)
    assert native.calls[-1][1]["model"] == "new-model"


@async_test
async def test_cancelled_native_await_retains_unknown_without_retry(tmp_path):
    operations, native, _peer, journal = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(params):
        entered.set()
        await release.wait()
        return {"turn": {"id": "physical", "status": "inProgress", "items": []}}

    native.responses["turn/start"] = slow
    params = {
        "conversationId": "t",
        "turnStart": {
            "request": {"threadId": "t", "input": [], "clientUserMessageId": "message"}
        },
    }
    running = asyncio.create_task(
        operations.handle("thread-follower-start-turn", params)
    )
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert journal.operation("t")["stage"] == "unknown"
    with pytest.raises(ValueError, match="unconfirmed"):
        await operations.handle("thread-follower-start-turn", params)
    release.set()
    await asyncio.gather(*operations.inflight)
    assert len(native.calls) == 1


@async_test
async def test_local_attachment_context_is_prepared_without_dropping_display_record(
    tmp_path,
):
    operations, native, peer, _journal = setup(tmp_path)
    native.responses["turn/start"] = {
        "turn": {"id": "physical", "status": "inProgress", "items": []}
    }
    attachment = {
        "path": "/workspace/photo.png",
        "name": "photo.png",
        "mediaType": "image/png",
        "future": 1,
    }
    await operations.handle(
        "thread-follower-start-turn",
        {
            "conversationId": "t",
            "turnStart": {
                "request": {
                    "threadId": "t",
                    "input": [{"type": "text", "text": "look"}],
                },
                "context": {"attachments": [attachment]},
            },
        },
    )
    assert native.calls[-1][1]["input"][-1] == {
        "type": "localImage",
        "path": "/workspace/photo.png",
    }
    assert peer.state["turns"][0]["params"]["attachments"][0]["future"] == 1


@async_test
async def test_unknown_setting_is_not_acknowledged_as_applied(tmp_path):
    operations, native, _peer, _journal = setup(tmp_path)
    with pytest.raises(ValueError, match="unsupported.*futureSetting"):
        await operations.handle(
            "thread-follower-update-thread-settings",
            {
                "conversationId": "t",
                "threadSettings": {"futureSetting": "ignored-by-native"},
            },
        )
    assert native.calls == []


@async_test
async def test_steer_service_tier_override_is_not_silently_ignored(tmp_path):
    operations, native, _peer, _journal = setup(
        tmp_path, [{"id": "active", "status": "inProgress", "items": []}]
    )
    with pytest.raises(ValueError, match="serviceTier"):
        await operations.handle(
            "thread-follower-steer-turn",
            {
                "conversationId": "t",
                "input": [{"type": "text", "text": "continue"}],
                "serviceTier": "fast",
            },
        )
    assert native.calls == []
