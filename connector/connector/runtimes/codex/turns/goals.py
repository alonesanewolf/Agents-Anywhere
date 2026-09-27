"""Native goal grammar and authoritative display; physical turns stay independent."""

import json
import re
from copy import deepcopy

STATUSES = {"active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"}


def parse_goal(text):
    parts = re.split(r"\s+", text.strip(), maxsplit=1)
    action, tail = parts[0], parts[1] if len(parts) > 1 else ""
    action = action or "status"
    tail = tail.strip()
    if action in {"status", "pause", "resume", "clear"}:
        if tail:
            raise ValueError("This goal action takes no arguments")
        return action, {
            "status": "paused" if action == "pause" else "active"
        } if action in {"pause", "resume"} else {}
    if action == "budget":
        payload = {"tokenBudget": json.loads(tail)}
    elif action in {"create", "edit", "set"}:
        payload = (
            json.loads(tail)
            if tail.startswith("{") or action == "set"
            else {"objective": tail}
        )
        if (
            not isinstance(payload, dict)
            or not payload
            or set(payload) - {"objective", "status", "tokenBudget"}
        ):
            raise ValueError(
                "Goal payload requires objective, status or tokenBudget only"
            )
        if action in {"create", "edit"} and "objective" not in payload:
            raise ValueError("An objective is required")
        if action == "create":
            payload.setdefault("status", "active")
    else:
        raise ValueError("Unknown goal action")
    if "objective" in payload and (
        not isinstance(payload["objective"], str) or not payload["objective"].strip()
    ):
        raise ValueError("Objective must be nonempty text")
    if "status" in payload and payload["status"] not in STATUSES:
        raise ValueError("Invalid goal status")
    budget = payload.get("tokenBudget")
    if budget is not None and (type(budget) is not int or not 0 < budget <= 2**63 - 1):
        raise ValueError("Token budget must be a positive integer or null")
    return action, payload


def validate_goal(goal, thread_id):
    if goal is not None and (
        not isinstance(goal, dict)
        or goal.get("threadId") != thread_id
        or not isinstance(goal.get("objective"), str)
        or not isinstance(goal.get("status"), str)
    ):
        raise ValueError("Invalid native goal observation")
    return deepcopy(goal)


async def publish_goal(states, session_id, thread_id, goal):
    state = states.get(session_id)
    previous = dict(state.metadata.get("codexPresentation", {})) if state else {}
    previous["threadGoal"] = deepcopy(goal)
    if goal is not None:
        previous["completedThreadGoal"] = (
            deepcopy(goal) if goal.get("status") == "complete" else None
        )
    await states.update(
        session_id,
        thread_id,
        status=state.status if state else "idle",
        metadata={"codexPresentation": previous},
    )


async def hydrate_goal(client, states, session_id, thread_id):
    facts = getattr(client, "command_capabilities", lambda _: {})(thread_id)
    if facts.get("coordinated") and facts.get("role") != "owner":
        return  # Follower's sole authority is its canonical owner state.
    native = getattr(client, "native_request", None)
    if not callable(native):
        return
    before = states.get(session_id)
    before_goal = (
        deepcopy(before.metadata.get("codexPresentation", {})) if before else {}
    )
    result = await native("thread/goal/get", {"threadId": thread_id})
    after = states.get(session_id)
    if (
        not facts.get("coordinated")
        and after
        and after.metadata.get("codexPresentation", {}) != before_goal
    ):
        return
    if "goal" not in result:
        raise ValueError("Native goal was not observed")
    goal = validate_goal(result["goal"], thread_id)
    observe = getattr(client, "observe_goal", None)
    if callable(observe) and not facts.get("goalObservationOwned"):
        await observe(thread_id, goal)
    await publish_goal(states, session_id, thread_id, goal)
