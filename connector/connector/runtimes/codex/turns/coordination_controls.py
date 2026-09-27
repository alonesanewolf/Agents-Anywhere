"""Exact coordinated control seam; user-facing aliases/catalog belong elsewhere."""

from collections.abc import Mapping

from connector.runtime_protocol import RuntimeUnsupportedError

CONTROL_OPERATIONS = frozenset(
    {
        "load-complete-history",
        "compact-thread",
        "update-thread-settings",
        "update-daybreak",
        "edit-last-user-turn",
        "set-queued-follow-ups-state",
    }
)


async def execute_coordination_control(client, thread_id, operation, payload):
    if (
        not callable(getattr(client, "owner_operation", None))
        or operation not in CONTROL_OPERATIONS
    ):
        raise RuntimeUnsupportedError(operation)
    if not isinstance(payload, Mapping):
        raise TypeError("Codex control payload must be an object")
    if any(
        key in payload
        for key in (
            "conversationId",
            "threadId",
            "hostId",
            "requestId",
            "responseContext",
        )
    ):
        raise ValueError("Codex control payload cannot override authority identity")
    if operation == "load-complete-history":
        if payload:
            raise ValueError("load-complete-history takes no arguments")
        state = await client.load_complete_history(thread_id)
        return {"state": state}
    return await client.owner_operation(
        thread_id, "thread-follower-" + operation, dict(payload)
    )
