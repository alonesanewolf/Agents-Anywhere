"""Serialized settings, guarded interruption and prevalidated destructive edit."""

import asyncio
from copy import deepcopy

from .context import prepare_start, require_feature
from .history import hydrate
from .projection import active_turn, native_to_state
from .state import enumerate_turns, history_complete


async def settings(operations, thread_id, params):
    state = operations.state(thread_id)
    values = deepcopy(params["threadSettings"])
    if "threadId" in values:
        raise ValueError("threadSettings cannot override threadId")
    supported = {
        "model",
        "effort",
        "collaborationMode",
        "approvalPolicy",
        "approvalsReviewer",
        "sandboxPolicy",
        "activePermissionProfile",
        "cwd",
        "serviceTier",
        "permissions",
        "runtimeWorkspaceRoots",
    }
    unknown = set(values) - supported
    if unknown:
        raise ValueError(f"unsupported thread settings: {', '.join(sorted(unknown))}")
    active_id = params.get("activeTurnId")
    condition = params.get("condition")
    missing = object()
    if (
        active_id is None
        and condition is not None
        and (
            state.get("latestReasoningEffort", missing)
            != condition.get("ifEffortEquals", missing)
            or (
                condition.get("ifModelEquals") is not None
                and state.get("latestModel") != condition["ifModelEquals"]
            )
        )
    ):
        return {"applied": False}
    if active_id is not None:
        active = active_turn(state)
        if active is None or active.get("turnId") != active_id:
            raise ValueError("active settings turn mismatch")
        if values.get("approvalsReviewer") is not None:
            require_feature(operations.sdk, "reviewer")
    before = dict(operations.settings_epochs[thread_id])
    result = await operations.call(
        thread_id, "thread/settings/update", {"threadId": thread_id, **values}
    )
    if result.get("applied") is False:
        return {"applied": False}
    # Record confirmed next-turn settings even if the active reviewer update fails.
    state = operations.state(thread_id)
    operations.merge_confirmed_settings(thread_id, state, values, before)
    await operations.peer.publish_state(thread_id, state)
    if active_id is not None and values.get("approvalsReviewer") is not None:
        await operations.call(
            thread_id,
            "turn/settings/update",
            {
                "threadId": thread_id,
                "turnId": active_id,
                "approvalsReviewer": values["approvalsReviewer"],
            },
        )
    return {"applied": True}


async def daybreak(operations, thread_id, params):
    enabled = params["daybreakEnabled"]
    if type(enabled) is not bool:
        raise ValueError("daybreakEnabled must be boolean")
    state = operations.state(thread_id)
    if not state.get("ephemeral"):
        require_feature(operations.sdk, "daybreak")
        result = await operations.call(
            thread_id,
            "thread/metadata/update",
            {"threadId": thread_id, "daybreakEnabled": enabled},
        )
        if result.get("thread", {}).get("daybreakEnabled") is not enabled:
            raise ValueError("native daybreak update was not confirmed")
    state = operations.state(thread_id)
    state["daybreakEnabled"] = enabled
    await operations.peer.publish_state(thread_id, state)
    return {"ok": True}


async def pause_goal(operations, thread_id):
    await operations.goal_request(
        thread_id, "thread/goal/set", {"threadId": thread_id, "status": "paused"}
    )


async def interrupt(operations, thread_id, params):
    state = operations.state(thread_id)
    active = active_turn(state)
    turn_id = active.get("turnId") if active else None
    expected = params.get("expectedTurnId")
    if expected is not None and expected != turn_id:
        return {"ok": True, "interruptedTurnId": None}
    mode = params.get("mode", "system")
    if mode not in ("system", "user-stop", "descendant-cleanup"):
        raise ValueError("unsupported interrupt mode")
    goal = state.get("threadGoal") or {}
    pause_error = None
    if (
        expected is None
        and goal.get("status") == "active"
        and mode != "descendant-cleanup"
    ):
        try:
            if mode == "user-stop":
                async with asyncio.timeout(0.5):
                    await pause_goal(operations, thread_id)
            else:
                await pause_goal(operations, thread_id)
        except Exception:
            if mode != "user-stop":
                raise
            pause_error = "Native goal pause outcome is unknown"
    if turn_id is not None:
        # Never retarget an intervening turn even for unguarded user stop.
        current = active_turn(operations.state(thread_id))
        if current is None or current.get("turnId") != turn_id:
            raise ValueError("active turn changed before interrupt")
        await operations.call(
            thread_id, "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
    if (
        expected is None
        and goal.get("status") == "active"
        and mode == "descendant-cleanup"
    ):
        try:
            await pause_goal(operations, thread_id)
        except Exception:  # noqa: BLE001 - preserve confirmed interrupt outcome
            pause_error = "Native goal pause outcome is unknown"
    from .queue import pause_queue

    await pause_queue(
        operations, thread_id, "Interrupted before the steer was accepted."
    )
    result = {"ok": True, "interruptedTurnId": turn_id}
    if expected is None and goal.get("status") == "active":
        current_goal = operations.state(thread_id).get("threadGoal")
        current_status = current_goal.get("status") if current_goal else None
        stopped = current_goal is None or current_status in {
            "paused",
            "blocked",
            "usageLimited",
            "budgetLimited",
            "complete",
        }
        same_goal = bool(current_goal) and all(
            current_goal.get(key) == goal.get(key) for key in ("objective", "createdAt")
        )
        result.update(
            goalPaused=pause_error is None and same_goal and current_status == "paused",
            goalStopped=stopped,
            goalStatus=current_status,
        )
        if not stopped:
            pause_error = pause_error or "Native goal pause outcome is unknown"
    if pause_error:
        result["goalPauseError"] = pause_error
    return result


async def edit(operations, thread_id, params):
    record = operations.journal.operation(thread_id) or {}
    if record.get("stage") == "rejected" and record.get("historyChanged"):
        if record.get("edit") != params:
            raise ValueError("edit recovery requires the same edit request")
        await operations.start(thread_id, record["restart"])
        return {"ok": True}
    if params.get("shouldSendPermissionOverrides"):
        raise ValueError("unsupported IDE permission override preparation")
    if params.get("writingBlockContextPrepared"):
        raise ValueError("unsupported writing block prompt replacement")
    state = operations.state(thread_id)
    if not history_complete(state):
        await hydrate(operations, thread_id)
        state = operations.state(thread_id)
    turns = enumerate_turns(state)
    index = next(
        (
            i
            for i, turn in enumerate(turns)
            if turn.get("turnId") == params.get("turnId")
        ),
        None,
    )
    if index is None or params.get("turnId") is None:
        raise ValueError("editable turn not found")
    if any(turn.get("status") == "inProgress" for turn in turns[index:]) or any(
        turn.get("params", {}).get("input") for turn in turns[index + 1 :]
    ):
        raise ValueError("only last user turn may be edited")
    request = deepcopy(turns[index]["params"])
    inputs = request.get("input", [])
    text = next((item for item in inputs if item.get("type") == "text"), None)
    if text is None or not isinstance(params.get("message"), str):
        raise ValueError("edit requires a plain user text input")
    if text.get("text_elements") or text.get("textElements"):
        raise ValueError("unsupported prepared prompt replacement")
    text["text"] = params["message"]
    request["turnTrigger"] = "edit_user_message"
    request.pop("clientUserMessageId", None)
    context = {
        "attachments": request.pop("attachments", []),
        "inheritThreadSettings": True,
    }
    for field in ("commentAttachments", "mcpAppModelContextAttachments"):
        if request.get(field):
            raise ValueError(f"unsupported edited context: {field}")
        request.pop(field, None)
    for key in ("serviceTier", "additionalContext"):
        if key in params:
            request[key] = deepcopy(params[key])
    turn_start = {"request": request, "context": context}
    prepare_start(thread_id, turn_start, state, operations.sdk)
    await operations.journal.begin(
        thread_id, {"edit": deepcopy(params), "restart": turn_start}
    )
    await operations.journal.stage(thread_id, "prepared", edit=deepcopy(params))
    if state.get("turnHistory", {}).get("kind") == "canonical":
        require_feature(operations.sdk, "revert")
        result = await operations.mutation(
            thread_id,
            "reverting",
            "thread/revert",
            {"threadId": thread_id, "beforeTurnId": params["turnId"]},
        )
    else:
        result = await operations.mutation(
            thread_id,
            "rolling-back",
            "thread/rollback",
            {"threadId": thread_id, "numTurns": len(turns) - index},
        )
    await operations.journal.stage(thread_id, "reverted", historyChanged=True)
    new_state = native_to_state(
        result["thread"], complete=False, host_id=operations.peer.host_id
    )
    new_state["requests"] = []
    await operations.peer.publish_state(thread_id, new_state)
    await operations.journal.set_passive_key(thread_id, None)
    if state.get("turnHistory", {}).get("kind") == "canonical":
        await hydrate(operations, thread_id)
    await operations.start(thread_id, turn_start, already_begun=True)
    return {"ok": True}
