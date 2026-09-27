"""AA commands mapped to concrete native operations and existing selectors."""

from connector.runtime_protocol import RuntimeCommand

ONLINE = ("idle", "error", "running", "waiting", "waiting_approval", "blocked")
IDLE = ("idle", "error")
ALIASES = {
    "compact-thread": "compact",
    "load-complete-history": "history",
    "refresh": "history",
    "plan-mode": "plan",
    "update-thread-settings": "settings",
    "update-daybreak": "daybreak",
    "edit-last-user-turn": "edit",
    "set-queued-follow-ups-state": "queue",
}
OPERATIONS = {
    "compact": "compact-thread",
    "history": "load-complete-history",
    "settings": "update-thread-settings",
    "daybreak": "update-daybreak",
    "edit": "edit-last-user-turn",
    "queue": "set-queued-follow-ups-state",
}
HINTS = {
    "goal": "status | create <objective or JSON> | edit <objective or JSON> | pause | resume | clear | budget <integer|null> | set <JSON>",
    "review": "uncommitted | branch <name> | commit <sha> | custom <instructions>",
    "plan": "on | off",
    "settings": "<JSON threadSettings payload>",
    "daybreak": '<JSON: {"daybreakEnabled":true}>',
    "edit": "<JSON native edit payload>",
    "queue": "<JSON native queue payload>",
}


def command_facts(client, thread_id, state):
    getter = getattr(client, "command_capabilities", None)
    facts = dict(getter(thread_id)) if callable(getter) and thread_id else {}
    facts["available"] = bool(client and thread_id) and not (
        state and state.metadata.get("codexCoordination", {}).get("available") is False
    )
    facts["status"] = state.status if state else "unknown"
    facts["goal"] = (
        state.metadata.get("codexPresentation", {}).get("threadGoal") if state else None
    )
    facts["nativeModel"] = (
        state.metadata.get("codexSettings", {}).get("latestModel") if state else None
    )
    return facts


def owner_reason(facts):
    if not facts["available"]:
        return "codex_unavailable"
    if facts.get("role") != "owner":
        return "goal_requires_aa_owner"
    if not facts.get("nativeControls"):
        return "native_controls_unavailable"
    return None


def list_codex_commands(
    external_session_id, client_available, query=None, limit=50, *, facts=None
):
    facts = facts or {
        "available": bool(external_session_id and client_available),
        "status": "unknown",
    }
    available = facts["available"]
    owned = facts.get("role") in {"owner", "follower"}
    coordinated = facts.get("coordinated", False)
    status = facts["status"]
    base_reason = None if available else "codex_unavailable"
    route_reason = base_reason or (None if owned else "no_native_owner")
    control_reason = route_reason or (None if coordinated else "coordination_required")
    native_reason = owner_reason(facts)
    result = []
    for name in (
        "status",
        "compact",
        "goal",
        "review",
        "model",
        "reasoning",
        "permission",
        "plan",
        "history",
        "settings",
        "daybreak",
        "edit",
        "queue",
    ):
        statuses = IDLE if name in {"compact", "review", "edit", "plan"} else ONLINE
        if name in {"status", "goal", "history"}:
            statuses = (*ONLINE, "unknown")
        reason = base_reason
        if name in {"compact", "model", "reasoning", "permission"}:
            reason = route_reason
        if name in {"plan", "settings", "daybreak", "edit", "queue"}:
            reason = control_reason
        if (
            name == "plan"
            and reason is None
            and (
                not isinstance(facts.get("nativeModel"), str)
                or not facts["nativeModel"]
            )
        ):
            reason = "native_model_unknown"
        if name == "history":
            reason = base_reason or (None if coordinated else "coordination_required")
        if name == "review":
            reason = native_reason
        if reason is None and status not in statuses:
            reason = "session_" + status
        metadata = {
            "ui": {
                "kind": "execute",
                "allowedStatuses": list(statuses),
                "acceptsMultiline": name in HINTS,
            }
        }
        if name in HINTS:
            metadata["ui"]["argumentHint"] = HINTS[name]
        if name in {"model", "reasoning", "permission"}:
            metadata["ui"] = {"kind": "selector", "target": name}
        if name == "goal":
            goal_reason = native_reason or (
                "native_goal_unobserved"
                if facts.get("goalSupported") is False
                else None
            )
            goal = facts.get("goal")
            goal_status = goal.get("status") if isinstance(goal, dict) else None
            actions = {}
            for action in (
                "create",
                "edit",
                "pause",
                "resume",
                "clear",
                "budget",
                "set",
            ):
                action_reason = goal_reason
                if action_reason is None:
                    if (
                        action in {"edit", "pause", "resume", "clear", "budget"}
                        and goal is None
                    ):
                        action_reason = "no_native_goal"
                    elif action == "pause" and goal_status != "active":
                        action_reason = "goal_not_active"
                    elif action == "resume" and goal_status not in {
                        "paused",
                        "blocked",
                        "usageLimited",
                        "budgetLimited",
                    }:
                        action_reason = "goal_not_resumable"
                actions[action] = {
                    "enabled": action_reason is None,
                    "disabledReason": action_reason,
                }
            metadata["goalActions"] = actions

        result.append(
            RuntimeCommand(
                id=name,
                title=name.capitalize(),
                description=HINTS.get(name, f"Codex {name}"),
                aliases=tuple(
                    alias for alias, target in ALIASES.items() if target == name
                ),
                enabled=reason is None,
                disabled_reason=reason,
                accepts_args=name in HINTS,
                args_schema={"type": "string"} if name in HINTS else None,
                metadata=metadata,
            )
        )
    needle = (query or "").strip().casefold()
    return tuple(
        c
        for c in result
        if not needle
        or needle
        in " ".join((c.id, c.title, c.description or "", *c.aliases)).casefold()
    )[: max(0, min(limit, 1000))]
