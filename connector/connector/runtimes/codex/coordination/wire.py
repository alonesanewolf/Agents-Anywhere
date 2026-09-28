"""Codex IDE coordination envelopes, distinct from app-server JSON-RPC."""

import asyncio
import json
import struct
from typing import Any

from farfield_python import METHOD_VERSIONS as DESKTOP_METHOD_VERSIONS

MAX_FRAME_BYTES = 256 * 1024 * 1024
INITIALIZING_CLIENT = "initializing-client"
METHOD_VERSIONS = {
    # AA's local router negotiates initialize separately at version 0.
    **{
        name: version
        for name, version in DESKTOP_METHOD_VERSIONS.items()
        if name != "initialize"
    },
    "thread-stream-following-status-requested": 1,
    "ipc-connection-reset": 1,
    "thread-read-state-changed": 3,
    "thread-archived": 2,
    "thread-unarchived": 1,
    "thread-follower-update-daybreak": 1,
    "thread-follower-edit-last-user-turn": 2,
    "thread-follower-set-queued-follow-ups-state": 1,
    "thread-queued-followups-changed": 2,
}


class IpcError(Exception):
    """A stable protocol/lifecycle error, without private payload details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def method_version(method: str) -> int:
    return METHOD_VERSIONS.get(method, 0)


def request_version(method: str, params: Any = None, host_id: str | None = None) -> int:
    if host_id is not None and method.startswith("thread-follower-"):
        return method_version(method) + 1
    if (
        method == "thread-follower-interrupt-turn"
        and isinstance(params, dict)
        and params.get("expectedTurnId") is None
    ):
        return 3
    return method_version(method)


def request_version_matches(
    method: str, version: int, host_id: str | None = None
) -> bool:
    return version == request_version(method, host_id=host_id) or (
        host_id is None and method == "thread-follower-interrupt-turn" and version == 3
    )


def encode_frame(envelope: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise IpcError("invalid-frame") from exc
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise IpcError("invalid-frame-length")
    return struct.pack("<I", len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    length = struct.unpack("<I", await reader.readexactly(4))[0]
    if not 0 < length <= MAX_FRAME_BYTES:
        raise IpcError("invalid-frame-length")
    try:
        payload = await reader.readexactly(length)
        envelope = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IpcError("invalid-frame") from exc
    if not isinstance(envelope, dict):
        raise IpcError("invalid-envelope")
    return envelope


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON numeric constant")


async def send_frame(writer: asyncio.StreamWriter, envelope: dict[str, Any]) -> None:
    # A single write prevents interleaving when independent handler tasks reply.
    writer.write(encode_frame(envelope))
    await writer.drain()
