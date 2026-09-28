"""Ordered physical activity, separate from raw turn history and goal state.

An explicit native idle invalidates prior in-progress targets without rewriting
those historical turns. Later starts reactivate only their observed target.
"""

KEY = "aaActivity"


def native_status(state):
    value = state.get("threadRuntimeStatus", state.get("status"))
    return value.get("type") if isinstance(value, dict) else value


def activity(state, turns=None):
    observed = state.get(KEY)
    if isinstance(observed, dict):
        return observed
    turns = state.get("turns", []) if turns is None else turns
    ids = (
        []
        if native_status(state) == "idle"
        else [
            turn.get("turnId", turn.get("id"))
            for turn in turns
            if turn.get("status") == "inProgress"
        ]
    )
    return {
        "sequence": 0,
        "turnIds": ids,
        "running": bool(ids) or native_status(state) == "active",
    }


def observe_activity(
    state,
    *,
    turns,
    status=None,
    started=None,
    completed=None,
    terminal_transition=False,
):
    previous = activity(state, turns)
    ids = list(previous["turnIds"])
    running = previous["running"]
    if status == "idle":
        ids, running = [], False
    elif status == "active":
        running = True
    if started is not None:
        ids = [value for value in ids if value != started] + [started]
        running = True
    if completed is not None:
        if completed in ids:
            ids = [value for value in ids if value != completed]
            running = bool(ids)
        elif not terminal_transition:
            # A historical correction cannot erase unrelated status-only active
            # evidence or invalidate an unrelated acknowledged turn.
            return
    state[KEY] = {
        "sequence": previous["sequence"] + 1,
        "turnIds": ids,
        "running": running,
    }


def activity_signature(state, turns):
    """History text, pagination, goals and settings are not activity evidence."""
    return (
        native_status(state),
        state.get(KEY),
        tuple(
            turn.get("turnId", turn.get("id"))
            for turn in turns
            if turn.get("status") == "inProgress"
        ),
    )


def activity_patch(patches):
    # Active-set transitions are compared by activity_signature. Only explicit
    # runtime status must count even when its value is repeated unchanged.
    return any(
        isinstance(patch.get("path"), list)
        and patch["path"][:1] in (["threadRuntimeStatus"], ["status"], [KEY])
        for patch in patches
    )


def is_running(state, turns=None):
    return activity(state, turns)["running"]
