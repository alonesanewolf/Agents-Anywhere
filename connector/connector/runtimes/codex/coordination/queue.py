"""Owner legacy queue persistence and head-only execution."""

from copy import deepcopy

from .context import queue_start
from .projection import active_turn
from .state import enumerate_turns


async def accept_queue(operations, thread_id, params):
    messages = deepcopy(params["state"].get(thread_id, []))
    if not isinstance(messages, list):
        raise TypeError("queue must contain message records")
    ids = set()
    for message in messages:
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("id"), str)
            or message["id"] in ids
        ):
            raise ValueError("invalid or duplicate queued message id")
        ids.add(message["id"])
        context = message.get("context") or {}
        if context.get("untrustedAppMessage") is not None or any(
            item.get("untrusted") is True
            for item in context.get("mcpAppModelContextAttachments", [])
        ):
            raise ValueError("App input requires confirmation before legacy delivery")
        try:
            queue_start(thread_id, message)
        except ValueError as exc:
            message.setdefault("pausedReason", str(exc))
    async with operations.queue_locks[thread_id]:
        operations.state(thread_id)
        await operations.journal.replace_queue(thread_id, messages)
        operations.state(thread_id)
        await publish_queue(operations, thread_id)
    return {"ok": True}


async def publish_queue(operations, thread_id):
    await operations.peer.publish_event(
        "thread-queued-followups-changed",
        {
            "conversationId": thread_id,
            "hostId": operations.peer.host_id,
            "messages": operations.journal.queue(thread_id),
        },
    )


async def pause_queue(operations, thread_id, reason):
    async with operations.queue_locks[thread_id]:
        messages = operations.journal.queue(thread_id)
        if not messages:
            return
        for message in messages:
            message.setdefault("pausedReason", reason)
        await operations.journal.replace_queue(thread_id, messages)
        await publish_queue(operations, thread_id)


async def execute_head(operations, thread_id):
    async with (
        operations.settings_locks[thread_id],
        operations.mutation_locks[thread_id],
    ):
        state = operations.state(thread_id)
        messages = operations.journal.queue(thread_id)
        if not messages or messages[0].get("pausedReason") or active_turn(state):
            return False
        turns = enumerate_turns(state)
        if turns and (
            turns[-1].get("status") != "completed"
            or not any(
                item.get("type") in ("agentMessage", "contextCompaction")
                for item in turns[-1].get("items", [])
            )
        ):
            return False
        head = messages[0]
        try:
            prepared = queue_start(thread_id, head)
            # No awaits between final head check and entering start's journal.
            operations.state(thread_id)
            if operations.journal.queue(thread_id)[:1] != [head]:
                return False
            token = operations.queue_head.set(head)
            try:
                await operations.start(thread_id, prepared)
            finally:
                operations.queue_head.reset(token)
        except Exception as exc:
            await pause_queue(operations, thread_id, str(exc) or type(exc).__name__)
            raise
        async with operations.queue_locks[thread_id]:
            current = operations.journal.queue(thread_id)
            if current and current[0]["id"] == head["id"]:
                await operations.journal.replace_queue(thread_id, current[1:])
                await publish_queue(operations, thread_id)
        return True
