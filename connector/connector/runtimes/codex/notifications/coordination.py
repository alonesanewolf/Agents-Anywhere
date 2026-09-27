"""Authoritative snapshot projection, never historical lifecycle event replay."""

from dataclasses import dataclass, field, replace
from hashlib import sha256

from connector.runtime_protocol import SessionNotice
from connector.runtimes.codex.domain.approvals import (
    approval_notice_from_request,
    is_approval_request,
)
from connector.runtimes.codex.domain.input_requests import question_action


def request_notice(session_id, thread_id, request, token):
    method, params = request["method"], request.get("params", {})
    if is_approval_request(method):
        notice = approval_notice_from_request(
            session_id, thread_id, method, params, request["id"], params.get("turnId")
        )
    else:
        question = method == "item/tool/requestUserInput"
        notice = SessionNotice(
            notice_id="",
            session_id=session_id,
            runtime="codex",
            type="interaction",
            title="Codex needs input" if question else "MCP server requests input",
            message=params.get("message"),
            interaction_type="input_request" if question else "elicitation",
            response_required=True,
            blocking={"scope": "session", "targetId": session_id},
            actions=(question_action(params),)
            if question
            else tuple(
                {"actionId": action, "label": action.title()}
                for action in ("accept", "decline", "cancel")
            ),
            context={
                "questions": params.get("questions", []),
                "requestedSchema": params.get("requestedSchema"),
                "mode": params.get("mode"),
                "url": params.get("url"),
            },
            source={"threadId": thread_id, "requestId": request["id"]},
        )
    return replace(
        notice,
        notice_id="notice_codex_"
        + sha256(f"{session_id}\0{token}".encode()).hexdigest(),
        context={**notice.context, "responseContext": token, "nativeRequest": request},
    )


@dataclass
class CoordinationSnapshotProjector:
    host: object
    session_states: object
    active_turn_ids: dict
    timeline: object
    notices: object
    read_selections: object = None
    published: dict = field(default_factory=dict)

    async def handle(self, session_id, thread_id, params):
        thread = params.get("thread")
        available = isinstance(thread, dict)
        contexts = params.get("requestContexts", []) if available else []
        pending = {}
        for context in contexts:
            request = next(
                (
                    r
                    for r in params.get("requests", [])
                    if type(r.get("id")) is type(context.get("requestId"))
                    and r.get("id") == context.get("requestId")
                    and r.get("method") == context.get("method")
                ),
                None,
            )
            if request is None:
                continue
            notice = request_notice(
                session_id, thread_id, request, context["responseContext"]
            )
            pending[notice.notice_id] = notice
        for notice in self.notices.current_for_session(session_id):
            if "responseContext" in notice.context and notice.notice_id not in pending:
                closed = self.notices.transition(
                    notice.notice_id,
                    status="closed",
                    response_required=False,
                    blocking=None,
                    actions=(),
                    metadata={"close_reason": "request_resolved_or_authority_changed"},
                )
                await self.host.notice_upsert(closed)
        for notice_id, notice in pending.items():
            existing = self.notices.get(notice_id)
            # Do not resurrect a consumed or currently dispatching response context.
            if existing is None:
                self.notices.upsert(notice)
                await self.host.notice_upsert(notice)
        metadata = {
            "source": "codex.coordination/state",
            "codexCoordination": {
                **params.get("coordination", {}),
                "available": available,
            },
            "codexCapabilities": params.get("capabilities", {}),
        }
        if not available:
            self.active_turn_ids.pop(session_id, None)
            await self.session_states.update(
                session_id, thread_id, status="blocked", metadata=metadata
            )
            return
        presentation = params.get("presentation")
        if isinstance(presentation, dict):
            cached = self.session_states.get(session_id)
            previous_display = (
                dict(cached.metadata.get("codexPresentation", {})) if cached else {}
            )
            for key in ("threadGoal", "completedThreadGoal"):
                if key in thread:
                    previous_display[key] = presentation.get(key)
            if previous_display:
                metadata["codexPresentation"] = previous_display
        metadata["codexSettings"] = {
            key: thread[key]
            for key in (
                "model",
                "effort",
                "approvalPolicy",
                "sandboxPolicy",
                "approvalsReviewer",
                "settings",
                "threadSettings",
                "latestTurnStartParams",
                "tokenUsage",
                "daybreakEnabled",
            )
            if key in thread
        }
        turns = thread.get("turns", [])
        active = next(
            (t for t in reversed(turns) if t.get("status") == "inProgress"), None
        )
        if active is not None:
            self.active_turn_ids[session_id] = active.get("id") or active.get("turnId")
        else:
            self.active_turn_ids.pop(session_id, None)
        status = (
            "waiting_approval"
            if self.notices.open_blocking_for_session(session_id)
            else "running"
            if active
            else "idle"
        )
        native_status = thread.get("status")
        if isinstance(native_status, dict):
            native_status = native_status.get("type")
        if not pending and native_status in ("systemError", "error"):
            status = "error"
        metadata["nativeStatus"] = thread.get("status")
        selections = (
            await self.read_selections(thread)
            if self.read_selections is not None
            else {}
        )
        await self.session_states.update(
            session_id,
            thread_id,
            status=status,
            selections=selections,
            metadata=metadata,
        )
        items = tuple(
            i
            for i in self.timeline.items_from_thread_snapshot(
                session_id, thread_id, thread, None
            )
            if i.type not in {"turn.start", "turn.end"}
        )
        current = {item.id: item for item in items}
        previous = self.published.get(thread_id, {})
        complete = params.get("canonicalComplete") is True
        removed = complete and bool(previous.keys() - current.keys())
        changed = tuple(item for item in items if previous.get(item.id) != item)
        if changed or removed or (complete and thread_id not in self.published):
            await self.host.timeline_sync(
                session_id=session_id,
                runtime="codex",
                external_session_id=thread_id,
                items=items if complete else changed,
                complete=complete,
                metadata={
                    "source": "codex.coordination/state",
                    "canonicalComplete": complete,
                },
            )
        self.published[thread_id] = current if complete else {**previous, **current}
