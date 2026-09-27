"""Raw paginated hydration without erasing live owner observations."""

from copy import deepcopy

from .projection import native_to_state
from .state import enumerate_turns


async def pages(call, method, params):
    result, seen = [], set()
    while True:
        page = await call(method, params)
        result.extend(deepcopy(page.get("data", [])))
        cursor = page.get("nextCursor")
        if cursor is None:
            return result
        if cursor in seen:
            raise ValueError("native history pagination repeated a cursor")
        seen.add(cursor)
        params = {**params, "cursor": cursor}


async def hydrate(operations, thread_id):
    before = operations.state(thread_id)
    raw = await operations.call(
        thread_id, "thread/read", {"threadId": thread_id, "includeTurns": False}
    )
    turns = await pages(
        lambda method, params: operations.call(thread_id, method, params),
        "thread/turns/list",
        {
            "threadId": thread_id,
            "limit": 100,
            "sortDirection": "asc",
            "itemsView": "full",
        },
    )
    for turn in turns:
        if (
            turn.get("itemsPagination", {}).get("hasLoadedOldest") is False
            or turn.get("itemsHasMore")
            or turn.get("itemsView") in ("notLoaded", "summary")
        ):
            entries = await pages(
                lambda method, params: operations.call(thread_id, method, params),
                "thread/items/list",
                {
                    "threadId": thread_id,
                    "turnId": turn["id"],
                    "sortDirection": "asc",
                    "limit": 100,
                },
            )
            turn["items"] = [entry["item"] for entry in entries]
            turn["itemsView"] = "full"
            turn["itemsPagination"] = {
                **turn.get("itemsPagination", {}),
                "hasLoadedOldest": True,
            }
    hydrated = native_to_state(
        {**raw["thread"], "turns": turns},
        complete=True,
        host_id=operations.peer.host_id,
    )
    current = operations.state(thread_id)
    # Keep non-history current settings/goals/requests and overlay turns actually
    # changed during the read; unchanged partial tails must not erase hydration.
    merged = {
        **hydrated,
        **{
            k: v
            for k, v in current.items()
            if k not in {"turns", "turnHistory", "turnsPagination"}
        },
    }
    merged["turns"] = hydrated["turns"]
    baseline = {t.get("turnId"): t for t in enumerate_turns(before)}
    indexed = {t.get("turnId"): t for t in merged["turns"]}
    for live in enumerate_turns(current):
        key = live.get("turnId")
        if live == baseline.get(key):
            continue
        old = indexed.get(key)
        if old is None:
            merged["turns"].append(live)
            continue
        items = {item["id"]: item for item in old.get("items", [])}
        items.update({item["id"]: item for item in live.get("items", [])})
        old.update(live)
        old["items"] = list(items.values())
        old["itemsPagination"] = {"hasLoadedOldest": True}
    return await operations.peer.publish_state(thread_id, merged)
