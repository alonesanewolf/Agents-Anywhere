import asyncio

import pytest
from test_codex_coordination_operations import async_test, setup


@async_test
async def test_queue_replacement_during_journal_write_cannot_send_removed_head(
    tmp_path,
):
    from connector.runtimes.codex.coordination.queue import execute_head

    operations, native, _peer, journal = setup(tmp_path)
    await operations.handle(
        "thread-follower-set-queued-follow-ups-state",
        {"conversationId": "t", "state": {"t": [{"id": "one", "text": "one"}]}},
    )
    original_stage = journal.stage
    ready, release = asyncio.Event(), asyncio.Event()

    async def stage(thread_id, stage, **fields):
        await original_stage(thread_id, stage, **fields)
        if stage == "starting":
            ready.set()
            await release.wait()

    journal.stage = stage
    task = asyncio.create_task(execute_head(operations, "t"))
    await ready.wait()
    await operations.handle(
        "thread-follower-set-queued-follow-ups-state",
        {"conversationId": "t", "state": {"t": []}},
    )
    release.set()
    with pytest.raises(ValueError, match="head changed"):
        await task
    assert native.calls == []


@async_test
async def test_queue_unknown_send_is_retained_and_not_repeated(tmp_path):
    from connector.runtimes.codex.coordination.queue import execute_head

    operations, native, _peer, journal = setup(tmp_path)
    native.responses["turn/start"] = TimeoutError("native outcome unknown")
    messages = [{"id": "one", "text": "one"}, {"id": "two", "text": "two"}]
    await operations.handle(
        "thread-follower-set-queued-follow-ups-state",
        {"conversationId": "t", "state": {"t": messages}},
    )
    with pytest.raises(TimeoutError):
        await execute_head(operations, "t")
    assert [message["id"] for message in journal.queue("t")] == ["one", "two"]
    assert journal.queue("t")[0]["pausedReason"] == "native outcome unknown"
    assert await execute_head(operations, "t") is False
    assert len(native.calls) == 1


@async_test
async def test_restart_after_confirmed_send_before_queue_removal_does_not_repeat(
    tmp_path,
):
    from connector.runtimes.codex.coordination.queue import execute_head

    operations, native, _peer, journal = setup(tmp_path)
    await operations.handle(
        "thread-follower-set-queued-follow-ups-state",
        {"conversationId": "t", "state": {"t": [{"id": "one", "text": "one"}]}},
    )
    await journal.begin(
        "t", {"clientUserMessageId": "one", "input": [{"type": "text", "text": "one"}]}
    )
    await journal.stage("t", "confirmed", result={"turn": {"id": "sent"}})
    assert await execute_head(operations, "t") is True
    assert journal.queue("t") == []
    assert native.calls == []
