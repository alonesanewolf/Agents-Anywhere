from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from connector.runtime_protocol import (
    RuntimeAttachment,
    RuntimeInvalidRequestError,
    RuntimeOperationResult,
    RuntimeSessionSourceStateCache,
    RuntimeSessionStateCache,
    RuntimeUnsupportedError,
    SessionSourceObservation,
)
from connector.runtime_protocol.host import RuntimeHostClient
from connector.runtimes.codex.domain.notices import CodexNoticeRegistry
from connector.runtimes.codex.domain.pending_messages import (
    PendingClientMessageRegistry,
)
from connector.runtimes.codex.domain.selections import (
    model_settings_from_selection,
    permission_settings_from_selection,
)
from connector.runtimes.codex.runtime_helpers import soft_codex_unavailable_reason
from connector.runtimes.codex.sdk.runtime_client import (
    CodexInterruptTurnRequest,
    CodexRuntimeClient,
    CodexStartTurnRequest,
    CodexSteerTurnRequest,
    CodexTurnInputAttachment,
)
from connector.runtimes.codex.sessions.inventory import (
    reconcile_codex_thread_availability,
)
from connector.runtimes.codex.turns.attachments import materialize_codex_attachments

EnsureStarted = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class CodexTurnActions:
    host: RuntimeHostClient
    client: CodexRuntimeClient | None
    session_states: RuntimeSessionStateCache
    source_states: RuntimeSessionSourceStateCache
    active_turn_ids: dict[str, str]
    notices: CodexNoticeRegistry
    ensure_started: EnsureStarted
    pending_messages: PendingClientMessageRegistry
    list_model_catalog: Callable[[str | None, int], Awaitable[Any]] | None = None
    list_permission_catalog: Callable[[str | None, int], Awaitable[Any]] | None = None

    async def start_turn(
        self,
        session_id: str,
        external_session_id: str | None,
        content: str,
        selections: Mapping[str, str | None] | None = None,
        attachments: tuple[RuntimeAttachment, ...] = (),
        client_message_id: str | None = None,
        cwd: str | None = None,
    ) -> RuntimeOperationResult:
        _ = cwd
        if self.client is None or external_session_id is None:
            raise RuntimeUnsupportedError("start_turn")
        cached_source = self.source_states.get(session_id)
        if cached_source is not None and cached_source.state.availability in {
            "archived",
            "unavailable",
            "deleted",
            "missing",
        }:
            return _source_failure_result(cached_source)
        await self.ensure_started()
        effective_selections = dict(selections or {})
        if not effective_selections and not callable(
            getattr(self.client, "owner_operation", None)
        ):
            cached = self.session_states.get(session_id)
            effective_selections = dict(cached.selections if cached is not None else {})
        try:
            selected_model = (
                await model_settings_from_selection(
                    effective_selections.get("model"),
                    self.list_model_catalog,
                )
                if self.list_model_catalog is not None
                else {}
            )
            native_permission = (
                await permission_settings_from_selection(
                    effective_selections.get("permission"),
                    self.list_permission_catalog,
                )
                if self.list_permission_catalog is not None
                else {}
            )
        except RuntimeInvalidRequestError as exc:
            return RuntimeOperationResult(
                ok=False,
                code="codex_invalid_selection",
                message=str(exc),
                result={"externalSessionId": external_session_id},
            )
        await self._set_session_state(
            session_id=session_id,
            external_session_id=external_session_id,
            status="waiting",
            selections=effective_selections,
            metadata={"source": "codex.turn/start.requested"},
        )
        self.pending_messages.register(
            external_session_id=external_session_id,
            client_message_id=client_message_id,
            text=content,
            attachments=attachments,
        )
        try:
            codex_attachments = await materialize_codex_attachments(
                self.host,
                session_id,
                attachments,
            )
            result = await self.client.start_turn(
                CodexStartTurnRequest(
                    thread_id=external_session_id,
                    content=content,
                    client_message_id=client_message_id,
                    model=selected_model.model,
                    effort=selected_model.effort,
                    approval_policy=native_permission.approval_policy,
                    approvals_reviewer=native_permission.approvals_reviewer,
                    sandbox=native_permission.sandbox,
                    attachments=codex_attachments,
                )
            )
        except Exception as exc:
            refresh = getattr(self.client, "refresh_state", None)
            if callable(refresh):
                # Restore known canonical selections after a rejected/unknown
                # dispatch; the requested preference is not an observation.
                await refresh(external_session_id, force=True)
            source_observation = await self._source_observation_from_error(
                session_id=session_id,
                external_session_id=external_session_id,
                error=exc,
            )
            if source_observation is not None:
                self.pending_messages.pending_message_by_client_id(
                    external_session_id,
                    client_message_id,
                )
                await self._set_session_state(
                    session_id=session_id,
                    external_session_id=external_session_id,
                    status="idle",
                    metadata={"source": "codex.turn/start.source-unavailable"},
                )
                return _source_failure_result(source_observation)
            await self._set_session_state(
                session_id=session_id,
                external_session_id=external_session_id,
                status="error",
                error={
                    "code": exc.__class__.__name__,
                    "message": str(exc) or exc.__class__.__name__,
                },
                metadata={"source": "codex.turn/start.failed"},
            )
            raise
        turn_id = result.turn_id
        current_state = self.session_states.get(session_id)
        latest = (
            current_state.metadata.get("codexLatestTurn") if current_state else None
        )
        canonical_terminal = (
            isinstance(latest, dict)
            and latest.get("id") == turn_id
            and latest.get("status")
            in {"completed", "failed", "interrupted", "cancelled"}
        )
        if callable(getattr(self.client, "owner_operation", None)):
            # The coordinated facade has already projected the current canonical
            # state. Its ACK can name an older physical turn. Never repaint it.
            self.pending_messages.bind_turn(
                external_session_id=external_session_id,
                client_message_id=client_message_id,
                turn_id=turn_id,
            )
            return RuntimeOperationResult(
                ok=True,
                result={
                    "turnId": turn_id,
                    "turn": dict(result.payload),
                    "externalSessionId": external_session_id,
                },
            )
        if canonical_terminal or turn_completed_before_start_returned(current_state):
            return RuntimeOperationResult(
                ok=True,
                result={
                    "turnId": turn_id,
                    "turn": dict(result.payload),
                    "externalSessionId": external_session_id,
                    "status": current_state.status
                    if current_state is not None
                    else None,
                    "completed": True,
                },
            )
        if turn_id is not None:
            self.active_turn_ids[session_id] = turn_id
            self.pending_messages.bind_turn(
                external_session_id=external_session_id,
                client_message_id=client_message_id,
                turn_id=turn_id,
            )
        await self._set_session_state(
            session_id=session_id,
            external_session_id=external_session_id,
            status="running",
            selections=None
            if callable(getattr(self.client, "owner_operation", None))
            else effective_selections,
            metadata={
                "source": "codex.turn/start",
                **({"turn_id": turn_id} if turn_id else {}),
            },
        )
        return RuntimeOperationResult(
            ok=True,
            result={
                "turnId": turn_id,
                "turn": dict(result.payload),
                "externalSessionId": external_session_id,
            },
        )

    async def _source_observation_from_error(
        self,
        *,
        session_id: str,
        external_session_id: str,
        error: Exception,
    ) -> SessionSourceObservation | None:
        message = str(error)
        normalized = message.lower()
        if "is archived" in normalized:
            availability = "archived"
        elif getattr(error, "code", None) == -32600 or "-32600" in normalized:
            try:
                availability = await reconcile_codex_thread_availability(
                    self.client,
                    external_session_id,
                )
            except Exception:  # noqa: BLE001
                return None
            if availability == "available":
                return None
        else:
            return None
        return await self.source_states.update(
            session_id=session_id,
            external_session_id=external_session_id,
            availability=availability,
            reason=message,
            observed_at=None,
            observation_origin="operation",
        )

    async def steer_turn(
        self,
        session_id: str,
        external_session_id: str | None,
        content: str,
        attachments: tuple[RuntimeAttachment, ...] = (),
        client_message_id: str | None = None,
    ) -> RuntimeOperationResult:
        if self.client is None or external_session_id is None:
            raise RuntimeUnsupportedError("steer_turn")
        turn_id = self.active_turn_ids.get(session_id)
        if turn_id is None:
            await self._set_session_state(
                session_id=session_id,
                external_session_id=external_session_id,
                status="idle",
                metadata={"source": "codex.turn/steer.no-active-turn"},
            )
            return RuntimeOperationResult(
                ok=False,
                code="codex_no_active_turn",
                message="Codex runtime has no active turn to steer",
                result={"externalSessionId": external_session_id},
            )
        await self.ensure_started()
        codex_attachments = await materialize_codex_attachments(
            self.host,
            session_id,
            attachments,
        )
        if len(codex_attachments) != len(attachments):
            raise RuntimeInvalidRequestError(
                "Steering attachment materialization failed"
            )
        client_message_id = client_message_id or str(uuid4())
        self.pending_messages.register(
            external_session_id=external_session_id,
            client_message_id=client_message_id,
            text=content,
            attachments=attachments,
            steering=True,
            turn_id=turn_id,
        )
        try:
            result = await self.client.steer_turn(
                CodexSteerTurnRequest(
                    thread_id=external_session_id,
                    turn_id=turn_id,
                    content=content,
                    attachments=codex_attachments,
                    client_message_id=client_message_id,
                )
            )
        except (RuntimeError, RuntimeInvalidRequestError) as exc:
            soft_reason = soft_codex_unavailable_reason(str(exc))
            if soft_reason is None:
                raise
            self.active_turn_ids.pop(session_id, None)
            await self._set_session_state(
                session_id=session_id,
                external_session_id=external_session_id,
                status="idle",
                metadata={
                    "source": "codex.turn/steer.soft-failed",
                    "reason": soft_reason,
                    "turn_id": turn_id,
                },
            )
            return RuntimeOperationResult(
                ok=False,
                code=soft_reason,
                message="Codex turn was unavailable to steer",
                result={"steered": False, "turnId": turn_id},
            )
        self.pending_messages.bind_turn(
            external_session_id=external_session_id,
            client_message_id=client_message_id,
            turn_id=result.turn_id,
        )
        if not callable(getattr(self.client, "owner_operation", None)):
            await self._set_session_state(
                session_id=session_id,
                external_session_id=external_session_id,
                status="running",
                metadata={"source": "codex.turn/steer", "turn_id": result.turn_id},
            )
        return RuntimeOperationResult(
            ok=True,
            result={
                "steered": True,
                "turnId": result.turn_id,
                "externalSessionId": external_session_id,
                "turn": dict(result.payload),
            },
        )

    async def interrupt_session(
        self,
        session_id: str,
        reason: str | None = None,
    ) -> RuntimeOperationResult:
        _ = reason
        state = self.session_states.get(session_id)
        external_session_id = state.external_session_id if state is not None else None
        turn_id = self.active_turn_ids.get(session_id)
        stop = getattr(self.client, "stop_session", None)
        if callable(stop) and external_session_id is not None:
            await self.ensure_started()
            try:
                result = await stop(external_session_id)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise ValueError("Invalid stop acknowledgement")
                if result.get("goalPauseError") or (
                    result.get("goalPaused") is False
                    and result.get("goalStopped") is not True
                ):
                    return RuntimeOperationResult(
                        ok=False,
                        code="goal_pause_unknown",
                        message="Turn interruption was acknowledged, but goal pause is unconfirmed. Refresh the session.",
                        result={
                            **result,
                            "executionState": "unknown",
                            "retryable": False,
                        },
                    )
                return RuntimeOperationResult(
                    ok=True, result={**result, "executionState": "accepted"}
                )
            except Exception:  # noqa: BLE001 - never retry an ambiguous session stop
                return RuntimeOperationResult(
                    ok=False,
                    code="stop_outcome_unknown",
                    message="Session stop outcome is unknown. Refresh the session.",
                    result={"executionState": "unknown", "retryable": False},
                )
        if (
            state
            and (
                state.metadata.get("codexPresentation", {}).get("threadGoal") or {}
            ).get("status")
            == "active"
        ):
            return RuntimeOperationResult(
                ok=False,
                code="goal_stop_unavailable",
                message="This backend cannot confirm a session-level goal stop.",
            )
        if turn_id is None:
            await self._set_session_state(
                session_id=session_id,
                external_session_id=external_session_id,
                status="idle",
                metadata={"source": "codex.session/interrupt.already-stopped"},
            )
            return RuntimeOperationResult(
                ok=True,
                result={
                    "interrupted": False,
                    "alreadyStopped": True,
                },
            )
        if self.client is None or external_session_id is None:
            raise RuntimeUnsupportedError("interrupt_session")
        await self.ensure_started()
        try:
            await self.client.interrupt_turn(
                CodexInterruptTurnRequest(
                    thread_id=external_session_id,
                    turn_id=turn_id,
                )
            )
        except (RuntimeError, RuntimeInvalidRequestError) as exc:
            soft_reason = soft_codex_unavailable_reason(str(exc))
            if soft_reason is None:
                raise
            self.active_turn_ids.pop(session_id, None)
            await self.close_blocking_notices_for_interrupted_session(session_id)
            await self._set_session_state(
                session_id=session_id,
                external_session_id=external_session_id,
                status="idle",
                metadata={
                    "source": "codex.session/interrupt.already-stopped",
                    "reason": soft_reason,
                },
            )
            return RuntimeOperationResult(
                ok=True,
                result={
                    "interrupted": False,
                    "alreadyStopped": True,
                },
            )
        self.active_turn_ids.pop(session_id, None)
        await self.close_blocking_notices_for_interrupted_session(session_id)
        await self._set_session_state(
            session_id=session_id,
            external_session_id=external_session_id,
            status="idle",
            metadata={"source": "codex.session/interrupt"},
        )
        return RuntimeOperationResult(
            ok=True,
            result={
                "interrupted": True,
                "alreadyStopped": False,
            },
        )

    async def close_blocking_notices_for_interrupted_session(
        self,
        session_id: str,
    ) -> None:
        for notice in self.notices.close_open_for_session(
            session_id=session_id,
            status="closed",
            reason="interrupted",
            source="codex.session/interrupt",
        ):
            await self.host.notice_upsert(notice)

    async def _set_session_state(
        self,
        session_id: str,
        external_session_id: str | None,
        status: str,
        selections: Mapping[str, str | None] | None = None,
        error: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        await self.session_states.update(
            session_id=session_id,
            external_session_id=external_session_id,
            status=status,  # type: ignore[arg-type]
            selections=selections,
            error=error,
            metadata=metadata,
        )


def turn_completed_before_start_returned(state: Any | None) -> bool:
    if state is None:
        return False
    if state.status not in {"idle", "blocked", "error"}:
        return False
    source = state.metadata.get("source")
    if not isinstance(source, str):
        return False
    return source in {
        "codex.turn/completed",
        "codex.turn/interrupted",
        "codex.turn/cancelled",
        "codex.turn/failed",
    }


def _source_failure_result(
    observation: SessionSourceObservation,
) -> RuntimeOperationResult:
    availability = observation.state.availability
    code = {
        "archived": "session_archived",
        "unavailable": "session_unavailable",
        "deleted": "session_deleted",
        "missing": "session_missing",
    }.get(availability, "session_unavailable")
    return RuntimeOperationResult(
        ok=False,
        code=code,
        message=f"Codex session is {availability}",
        result={"externalSessionId": observation.external_session_id},
        source_observation=observation,
    )


def content_with_codex_attachment_notes(
    content: str,
    attachments: tuple[CodexTurnInputAttachment, ...],
) -> str:
    if not attachments:
        return content
    notes = [
        attachment.reference_note()
        for attachment in attachments
        if not attachment.is_image
    ]
    if not notes:
        return content
    return "\n\n".join([content, *notes])
