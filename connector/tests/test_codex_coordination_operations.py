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
