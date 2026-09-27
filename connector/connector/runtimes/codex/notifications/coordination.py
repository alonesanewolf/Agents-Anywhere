"""Authoritative snapshot projection, never historical lifecycle event replay."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field, replace
from hashlib import sha256

from connector.runtime_protocol import SessionNotice
from connector.runtimes.codex.coordination.projection import state_to_native
from connector.runtimes.codex.coordination.requests import REQUEST_ROUTES
from connector.runtimes.codex.coordination.state import history_complete
from connector.runtimes.codex.domain import sessions as codex_sessions
from connector.runtimes.codex.domain.approvals import (
    approval_notice_from_request,
    is_approval_request,
)
from connector.runtimes.codex.domain.input_requests import (
    elicitation_actions,
    question_action,
)
from connector.runtimes.codex.domain.thread_state import thread_status


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
            interaction_type="input_request"
            if question or params.get("mode") == "form"
            else "elicitation",
            response_required=True,
            blocking={"scope": "session", "targetId": session_id},
            actions=(question_action(params),)
            if question
            else elicitation_actions(params),
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

    locks: dict = field(default_factory=lambda: defaultdict(asyncio.Lock))

    async def handle(self, session_id, thread_id, params):
        async with self.locks[thread_id]:
            await self._handle(session_id, thread_id, params)

    async def _handle(self, session_id, thread_id, params):
        thread = params.get("thread")
        available = isinstance(thread, dict)
        # Scoped passive-read cleanup has no new native facts. Preserve the last
        # snapshot; a followed/owned authority loss remains explicitly unavailable.
        if not available and params.get("coordination", {}).get("role") == "unattached":
            return
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
            if request is None or request.get("method") not in REQUEST_ROUTES.values():
                continue
            notice = request_notice(
                session_id, thread_id, request, context["responseContext"]
            )
            notice = replace(
                notice,
                metadata={
                    **notice.metadata,
                    "coordinationAuthority": {
                        key: params.get("coordination", {}).get(key)
                        for key in ("ownerClientId", "generation")
                    },
                },
            )
            pending[notice.notice_id] = notice
        for notice in self.notices.current_for_session(session_id):
            request = notice.context.get("nativeRequest", {})
            still_pending = any(
                type(r.get("id")) is type(request.get("id"))
                and r.get("id") == request.get("id")
                and r.get("method") == request.get("method")
                for r in params.get("requests", [])
            )
            same_authority = notice.metadata.get("coordinationAuthority") == {
                key: params.get("coordination", {}).get(key)
                for key in ("ownerClientId", "generation")
            }
            if (
                notice.status == "unknown"
                and available
                and still_pending
                and same_authority
            ):
                continue
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
        await self.host.session_meta_upsert(
            session_id=session_id,
            runtime="codex",
            external_session_id=thread_id,
            title=codex_sessions.thread_title(thread),
            cwd=codex_sessions.thread_cwd(thread),
            ordering_time=codex_sessions.thread_ordering_time(thread),
            metadata={"source": "codex.coordination/state"},
        )
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
                "latestModel",
                "latestReasoningEffort",
                "latestThreadSettings",
                "currentPermissions",
                "latestTokenUsageInfo",
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
            else thread_status(thread)
        )
        native_status = thread.get("status")
        if isinstance(native_status, dict):
            native_status = native_status.get("type")
        if not pending and native_status in ("systemError", "error"):
            status = "error"
        metadata["nativeStatus"] = thread.get("status")
        metadata["codexLatestTurn"] = (
            {
                "id": turns[-1].get("id") or turns[-1].get("turnId"),
                "status": turns[-1].get("status"),
            }
            if turns
            else None
        )
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
        await self._publish_timeline(
            session_id, thread_id, thread, params.get("canonicalComplete") is True
        )

    async def publish_history(self, session_id, thread_id, state):
        async with self.locks[thread_id]:
            await self._publish_timeline(
                session_id, thread_id, state_to_native(state), history_complete(state)
            )

    async def _publish_timeline(self, session_id, thread_id, thread, complete):
        items = tuple(
            i
            for i in self.timeline.items_from_thread_snapshot(
                session_id, thread_id, thread, None, preserve_native_ids=True
            )
            if i.type not in {"turn.start", "turn.end"}
        )
        current = {item.id: item for item in items}
        previous = self.published.get(thread_id, {})
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
