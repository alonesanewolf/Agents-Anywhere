"""Use stored, one-shot authority contexts for coordinated native responses."""

from connector.runtime_protocol import RuntimeOperationResult
from connector.runtimes.codex.coordination.requests import validate_response
from connector.runtimes.codex.domain.approvals import approval_response_from_interaction
from connector.runtimes.codex.domain.input_requests import question_response


async def respond(controller, session_id, notice_id, action_id, input_data):
    notice = controller.notices.get(notice_id)
    if (
        notice is None
        or notice.session_id != session_id
        or notice.status != "open"
        or not notice.response_required
        or not notice.context.get("responseContext")
    ):
        raise ValueError("Codex interaction is no longer available")
    if action_id not in {action.get("actionId") for action in notice.actions}:
        raise ValueError("Unsupported Codex interaction action")
    request = notice.context["nativeRequest"]
    method = request["method"]
    data = dict(input_data or {})
    if method == "item/tool/requestUserInput":
        payload = question_response(request["params"], data)
    elif method == "mcpServer/elicitation/request":
        payload = {
            "action": action_id,
            **{key: data[key] for key in ("content", "_meta") if key in data},
        }
    else:
        payload = dict(
            approval_response_from_interaction(action_id, notice.context).payload
        )
    # Same pure validator as the facade, before the first await or notice transition.
    # User input cannot replace native params, permission grants, method or identity.
    try:
        validate_response(request, payload)
    except (TypeError, ValueError) as exc:
        failed = controller.notices.transition(
            notice_id,
            status="open",
            metadata={
                "retryable": True,
                "responseOutcome": "validation_failed",
                "error": {"message": str(exc)},
            },
        )
        await controller.host.notice_upsert(failed)
        raise
    responding = controller.notices.transition(notice_id, status="responding")
    await controller.host.notice_upsert(responding)
    try:
        await controller.ensure_started()
        await controller.client.respond_to_request(
            notice.context["responseContext"], payload
        )
    except Exception as exc:
        failed = controller.notices.transition(
            notice_id,
            status="closed",
            response_required=False,
            blocking=None,
            actions=(),
            metadata={
                "retryable": False,
                "responseOutcome": "unknown",
                "error": {"message": str(exc), "code": type(exc).__name__},
            },
        )
        await controller.host.notice_upsert(failed)
        raise
    await controller._notice_resolved(notice_id, action_id, action_id, payload)
    cached = controller.session_states.get(session_id)
    if cached is not None:
        await controller.session_states.update(
            session_id,
            cached.external_session_id,
            status="waiting_approval"
            if controller.notices.open_blocking_for_session(session_id)
            else "running"
            if session_id in controller.active_turn_ids
            else "idle",
        )
    return RuntimeOperationResult(
        ok=True, result={"resolved": True, "noticeId": notice_id, "response": payload}
    )
