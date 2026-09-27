from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from openai_codex.generated.v2_all import Thread

from connector.runtimes.codex.sdk.events import CodexSdkEvent

CodexNotificationMessage = CodexSdkEvent | dict[str, Any]
NotificationHandler = Callable[[CodexNotificationMessage], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CodexTurnInputAttachment:
    name: str
    path: str
    media_type: str
    byte_size: int | None = None

    @property
    def is_image(self) -> bool:
        return self.media_type.startswith("image/")

    def reference_note(self) -> str:
        media_type = self.media_type or "unknown type"
        byte_size = f", {self.byte_size} bytes" if self.byte_size is not None else ""
        return f"[Attached file: {self.name} ({media_type}{byte_size}) at {self.path}]"


@dataclass(frozen=True, slots=True)
class CodexStartThreadRequest:
    cwd: str | None = None
    model: str | None = None
    approval_policy: str | None = None
    approvals_reviewer: str | None = None
    sandbox: str | None = None
    ephemeral: bool = False


@dataclass(frozen=True, slots=True)
class CodexStartTurnRequest:
    thread_id: str
    content: str
    client_message_id: str | None = None
    model: str | None = None
    effort: str | None = None
    approval_policy: str | None = None
    approvals_reviewer: str | None = None
    sandbox: str | None = None
    attachments: tuple[CodexTurnInputAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexResumeThreadRequest:
    thread_id: str
    model: str | None = None
    approval_policy: str | None = None
    approvals_reviewer: str | None = None
    sandbox: str | None = None


@dataclass(frozen=True, slots=True)
class CodexSteerTurnRequest:
    thread_id: str
    turn_id: str
    content: str
    client_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class CodexInterruptTurnRequest:
    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class CodexModelListResult:
    models: tuple[Mapping[str, Any], ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class CodexThreadListResult:
    threads: tuple[Mapping[str, Any], ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class CodexThreadReadResult:
    thread: Thread | Mapping[str, Any]
    canonical_complete: bool | None = None
    coordination_role: str | None = None


@dataclass(frozen=True, slots=True)
class CodexThreadTurnsResult:
    turns: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CodexThreadTurnsPage:
    turns: tuple[Mapping[str, Any], ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class CodexThreadResult:
    thread_id: str | None
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CodexTurnResult:
    turn_id: str | None
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CodexCompactResult:
    payload: Mapping[str, Any]


class CodexRuntimeClient(Protocol):
    async def start(self, handler: NotificationHandler) -> None: ...
    async def stop(self) -> None: ...
    async def list_models(self) -> CodexModelListResult: ...
    async def list_threads(
        self,
        limit: int = 100,
        cursor: str | None = None,
        archived: bool | None = None,
    ) -> CodexThreadListResult: ...
    async def read_thread(
        self,
        thread_id: str,
        include_turns: bool = True,
    ) -> CodexThreadReadResult: ...
    async def list_thread_turns(self, thread_id: str) -> CodexThreadTurnsResult: ...
    async def start_thread(
        self, request: CodexStartThreadRequest
    ) -> CodexThreadResult: ...
    async def start_turn(self, request: CodexStartTurnRequest) -> CodexTurnResult: ...
    async def steer_turn(self, request: CodexSteerTurnRequest) -> CodexTurnResult: ...
    async def interrupt_turn(
        self,
        request: CodexInterruptTurnRequest,
    ) -> CodexTurnResult: ...
    async def compact_thread(self, thread_id: str) -> CodexCompactResult: ...
    async def respond(
        self,
        request_id: str | int,
        result: Mapping[str, Any] | None = None,
    ) -> None: ...


class CoordinatedRuntimeClient(CodexRuntimeClient, Protocol):
    async def attach_thread(self, thread_id: str) -> Mapping[str, Any] | None: ...
    async def detach_thread(self, thread_id: str) -> None: ...
    def has_canonical_authority(self, thread_id: str) -> bool: ...
    def is_follower(self, thread_id: str) -> bool: ...
    async def respond_to_request(
        self, response_context: str, result: Mapping[str, Any]
    ) -> None: ...
    async def owner_operation(
        self, thread_id: str, method: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    async def load_complete_history(self, thread_id: str) -> Mapping[str, Any]: ...
    async def reconcile_thread(self, thread_id: str) -> bool: ...
    async def native_request(
        self, method: str, params: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    def native_runtime_info(self) -> Mapping[str, Any]: ...
