"""Pure ordered native events to current IDE ConversationState."""

from copy import deepcopy
from uuid import uuid4

from connector.runtimes.codex.domain.activity import observe_activity

from .projection import canonical_turn
from .settings import merge_settings
from .state import enumerate_turns


def exact_id(left, right):
    return type(left) is type(right) and left == right


def reduce_event(previous, message):
    state = deepcopy(previous)
    params = message.get("params") or {}
    method = message["method"]
    if "id" in message:
        state.setdefault("requests", []).append(deepcopy(message))
        return state
    if method == "serverRequest/resolved":
        state["requests"] = [
            r
            for r in state.get("requests", [])
            if not exact_id(r["id"], params.get("requestId"))
        ]
    elif method == "thread/goal/updated":
        goal = deepcopy(params.get("goal"))
        state["threadGoal"] = goal
        state["completedThreadGoal"] = (
            goal if goal and goal.get("status") == "complete" else None
        )
    elif method == "thread/goal/cleared":
        state["threadGoal"] = None
    elif method == "thread/tokenUsage/updated":
        state["latestTokenUsageInfo"] = deepcopy(params.get("tokenUsage"))
    elif method == "thread/status/changed":
        status = params.get("status")
        observe_activity(
            state,
            turns=enumerate_turns(state),
            status=status.get("type") if isinstance(status, dict) else status,
        )
        state["threadRuntimeStatus"] = deepcopy(status)
    elif method in ("thread/settings/updated", "thread/settings/changed"):
        settings = params.get("settings", params.get("threadSettings", {}))
        merge_settings(state, settings)
    elif method in ("thread/name/updated", "thread/metadata/updated"):
        state.update({k: deepcopy(v) for k, v in params.items() if k != "threadId"})
        if "threadName" in params:
            state["title"] = params["threadName"]
    elif method.startswith(("turn/", "item/")) and params.get(
        "turnId", (params.get("turn") or {}).get("id")
    ):
        turns = enumerate_turns(state)
        turn_id = params.get("turnId", (params.get("turn") or {}).get("id"))
        turn = next((t for t in turns if t.get("turnId") == turn_id), None)
        new_turn = turn is None
        if turn is None:
            turn = canonical_turn(
                {"id": turn_id, "items": [], "status": "inProgress"}, state["id"]
            )
            turns.append(turn)
        if method in ("turn/started", "turn/completed"):
            observe_activity(
                state,
                turns=turns,
                terminal_transition=method == "turn/completed"
                and (new_turn or turn.get("status") == "inProgress"),
                **{"started" if method == "turn/started" else "completed": turn_id},
            )
            raw = params["turn"]
            converted = canonical_turn(raw, state["id"])
            # Completion messages may omit historical items and prepared inputs.
            if "items" not in raw:
                converted.pop("items", None)
            if "params" not in raw:
                converted.pop("params", None)
                converted.pop("permissionParamsSource", None)
            if "items" in converted:
                items = {item["id"]: item for item in turn.get("items", [])}
                for item in converted["items"]:
                    items[item["id"]] = {**items.get(item["id"], {}), **item}
                converted["items"] = list(items.values())
            turn.update(converted)
            if method == "turn/completed":
                turn.setdefault("status", "completed")
        elif method == "turn/plan/updated":
            turn.setdefault("items", []).append(
                {
                    "id": str(uuid4()),
                    "type": "todo-list",
                    "explanation": params.get("explanation"),
                    "plan": deepcopy(params["plan"]),
                }
            )
        elif method == "turn/diff/updated":
            turn["diff"] = params.get("diff")
        elif "item" in params:
            item = deepcopy(params["item"])
            items = turn.setdefault("items", [])
            index = next(
                (
                    i
                    for i, value in enumerate(items)
                    if value.get("id") == item.get("id")
                ),
                None,
            )
            if index is None:
                items.append(item)
            else:
                items[index] = {**items[index], **item}
        elif method.endswith("/delta"):
            _delta(turn, method, params)
        else:
            turn.setdefault("nativeNotifications", {})[method] = deepcopy(params)
        state["turns"] = turns
    else:
        state.setdefault("nativeNotifications", {})[method] = deepcopy(params)
    return state


def _delta(turn, method, params):
    item_id = params.get("itemId")
    items = turn.setdefault("items", [])
    item = next((i for i in items if i.get("id") == item_id), None)
    kind = method.split("/")[1]
    if item is None:
        item = {"id": item_id, "type": kind}
        items.append(item)
    delta = params.get("delta", "")
    field = {
        "agentMessage": "text",
        "plan": "text",
        "commandExecution": "aggregatedOutput",
        "fileChange": "output",
    }.get(kind)
    if field:
        item[field] = item.get(field, "") + delta
    elif kind == "reasoning":
        field = "summary" if "summary" in method.lower() else "content"
        index = params.get("summaryIndex", params.get("contentIndex", 0))
        entries = item.setdefault(field, [])
        while len(entries) <= index:
            entries.append("")
        entries[index] += delta
    else:
        item.setdefault("nativeDeltas", []).append(deepcopy(params))
