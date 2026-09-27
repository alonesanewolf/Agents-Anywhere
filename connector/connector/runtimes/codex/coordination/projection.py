"""Lossless canonical/native projection; AA timeline is never a source of state."""

from copy import deepcopy

from .state import enumerate_turns


def canonical_turn(raw, thread_id):
    turn = deepcopy(raw)
    turn["turnId"] = raw.get("turnId", raw.get("id"))
    turn.setdefault("items", [])
    if raw.get("itemsView") in ("notLoaded", "summary"):
        turn["itemsPagination"] = {
            **turn.get("itemsPagination", {}),
            "hasLoadedOldest": False,
        }
    for source, target in (
        ("startedAt", "turnStartedAtMs"),
        ("completedAt", "finalAssistantStartedAtMs"),
    ):
        if isinstance(raw.get(source), (int, float)):
            turn[target] = raw[source] * 1000
    if "params" not in turn:
        first = None
        if turn.get("itemsPagination", {}).get("hasLoadedOldest") is not False:
            first = next(
                (item for item in turn["items"] if item.get("type") == "userMessage"),
                None,
            )
        turn["params"] = {
            "threadId": thread_id,
            "input": deepcopy(first.get("content", [])) if first else [],
        }
        if first and first.get("clientId") is not None:
            turn["params"]["clientUserMessageId"] = first["clientId"]
        turn["permissionParamsSource"] = "inferred"
    turn.setdefault("error", None)
    turn.setdefault("diff", None)
    return turn


def native_to_state(raw, *, complete=False, host_id="local"):
    state = deepcopy(dict(raw))
    state.update(
        hostId=host_id,
        requests=deepcopy(raw.get("requests", [])),
        resumeState="resumed",
    )
    for name in ("createdAt", "updatedAt", "recencyAt"):
        if isinstance(raw.get(name), (int, float)):
            state[name] = raw[name] * 1000
    state.setdefault("workspaceKind", "project")
    state.setdefault("title", raw.get("name") or raw.get("preview") or "")
    state["turns"] = [canonical_turn(turn, raw["id"]) for turn in raw.get("turns", [])]
    state["turnsPagination"] = {
        **state.get("turnsPagination", {}),
        "hasLoadedOldest": complete,
    }
    if raw.get("historyMode") == "paginated":
        entities = {turn["turnId"]: deepcopy(turn) for turn in state["turns"]}
        boundary = {
            "status": "exhausted" if complete else "available",
            "boundaryId": "native-history",
        }
        state["turnHistory"] = {
            "kind": "canonical",
            "history": {
                "entitiesByKey": entities,
                "generation": 0,
                "isComplete": complete,
                "islands": [
                    {
                        "id": "native-history",
                        "entries": [{"key": key, "value": key} for key in entities],
                        "olderBoundary": deepcopy(boundary),
                        "newerBoundary": {
                            "status": "exhausted",
                            "boundaryId": "native-latest",
                        },
                    }
                ],
            },
        }
    if "status" in raw:
        state["threadRuntimeStatus"] = deepcopy(raw["status"])
    return state


def state_to_native(state):
    thread = deepcopy(state)
    for name in ("createdAt", "updatedAt", "recencyAt"):
        if isinstance(state.get(name), (int, float)):
            thread[name] = state[name] / 1000
    thread["turns"] = enumerate_turns(state)
    for turn in thread["turns"]:
        turn["id"] = turn.get("turnId", turn.get("id"))
        for source, target in (
            ("turnStartedAtMs", "startedAt"),
            ("finalAssistantStartedAtMs", "completedAt"),
        ):
            if isinstance(turn.get(source), (int, float)):
                turn[target] = turn[source] / 1000
    if "threadRuntimeStatus" in state:
        thread["status"] = deepcopy(state["threadRuntimeStatus"])
    return thread


def presentation(state):
    plans = {}
    for turn in enumerate_turns(state):
        for item in turn.get("items", []):
            if item.get("type") == "todo-list" and "plan" in item:
                plans[turn["turnId"]] = {
                    **deepcopy(item),
                    "id": f"plan-progress:{turn['turnId']}",
                }
    return {
        "threadGoal": deepcopy(state.get("threadGoal")),
        "completedThreadGoal": deepcopy(state.get("completedThreadGoal")),
        "latestPlansByTurn": plans,
    }


def active_turn(state):
    return next(
        (
            turn
            for turn in reversed(enumerate_turns(state))
            if turn.get("status") == "inProgress"
        ),
        None,
    )
