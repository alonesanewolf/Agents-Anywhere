"""Stable per-turn plan progress and native Markdown plan artifacts."""

from copy import deepcopy
from hashlib import sha256

from connector.runtime_protocol import RuntimeTimelineItem, timeline_content_hash


def plan_item(session_id, thread_id, turn_id, raw, order_seq=0):
    progress = raw.get("type") == "todo-list"
    identity = (
        f"{thread_id}\0{turn_id}\0plan-progress"
        if progress
        else f"{thread_id}\0{turn_id}\0{raw.get('id')}"
    )
    content = (
        {
            "kind": "plan-progress",
            "explanation": raw.get("explanation"),
            "plan": deepcopy(raw.get("plan", [])),
        }
        if progress
        else {"kind": "plan", "text": raw.get("text", "")}
    )
    return RuntimeTimelineItem(
        id="codex_plan_" + sha256(identity.encode()).hexdigest()[:32],
        session_id=session_id,
        turn_id=turn_id,
        type="artifact",
        status="done",
        order_seq=order_seq,
        content=content,
        content_hash=timeline_content_hash(
            item_type="artifact", status="done", role=None, content=content
        ),
        source={
            "runtime": "codex",
            "threadId": thread_id,
            "itemId": raw.get("id"),
            "rawType": raw.get("type"),
        },
        metadata={"native": deepcopy(raw)},
    )


def plan_items(session_id, thread_id, thread):
    result = []
    for turn in thread.get("turns", []):
        turn_id = turn.get("id") or turn.get("turnId")
        latest = None
        for raw in turn.get("items", []):
            if raw.get("type") == "todo-list":
                latest = raw
            elif raw.get("type") == "plan":
                result.append(
                    plan_item(session_id, thread_id, turn_id, raw, len(result))
                )
        if latest is not None:
            result.append(
                plan_item(session_id, thread_id, turn_id, latest, len(result))
            )
    return tuple(result)
