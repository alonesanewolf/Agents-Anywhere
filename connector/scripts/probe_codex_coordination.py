"""Opt-in structural probe for an existing Codex App coordination socket.

The production peer and AA snapshot projector run in memory. No native SDK child,
router election, owner claim, or conversation operation is started here.
"""

import argparse
import asyncio
import json
import math
from pathlib import Path

from connector.runtime_protocol import RuntimeSessionStateCache
from connector.runtimes.codex.coordination.peer import CoordinationPeer
from connector.runtimes.codex.coordination.projection import (
    presentation,
    state_to_native,
)
from connector.runtimes.codex.coordination.state import (
    enumerate_turns,
    history_complete,
)
from connector.runtimes.codex.coordination.transport import CoordinationClient
from connector.runtimes.codex.coordination.wire import IpcError
from connector.runtimes.codex.domain.notices import CodexNoticeRegistry
from connector.runtimes.codex.notifications.coordination import (
    CoordinationSnapshotProjector,
)
from connector.runtimes.codex.timeline.accumulator import CodexTimelineAccumulator

_STATUSES = {
    "idle",
    "running",
    "waiting",
    "pending",
    "stopping",
    "waiting_approval",
    "error",
    "blocked",
    "unknown",
}


def _emit(**fields):
    print(
        json.dumps(fields, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    )


class _StructuralHost:
    """Accept the production projector's calls without retaining private content."""

    def __init__(self):
        self.status = "unknown"
        self.item_ids = set()

    async def session_meta_upsert(self, **_fields):
        pass

    async def session_state_update(self, *, status, **_fields):
        self.status = status if status in _STATUSES else "unknown"

    async def timeline_sync(self, *, items, complete, **_fields):
        ids = {item.id for item in items}
        self.item_ids = ids if complete else self.item_ids | ids

    async def notice_upsert(self, _notice):
        pass


def _error_code(exc):
    if isinstance(exc, IpcError):
        if exc.code in {
            "endpoint-not-found",
            "initialization-timeout",
            "not-connected",
        }:
            return "app-socket-unavailable"
        if exc.code in {"unsafe-endpoint", "unsafe-endpoint-parent"}:
            return "unsafe-endpoint"
        if exc.code in {"follow-timeout", "timeout", "connection-closed"}:
            return "follow-unavailable"
        if exc.code == "unsupported-platform":
            return "unsupported-platform"
    return "coordination-unavailable"


async def run_probe(
    home, thread_id, *, host_id="local", duration=2.0, connect_timeout=3.0
):
    """Observe one existing thread for a short interval; return a process exit code."""
    if (
        not isinstance(thread_id, str)
        or not thread_id.strip()
        or not isinstance(host_id, str)
        or not host_id.strip()
        or not math.isfinite(duration)
        or not 0 <= duration <= 30
        or not math.isfinite(connect_timeout)
        or not 0 < connect_timeout <= 10
    ):
        _emit(event="error", code="invalid-arguments")
        return 2

    client = CoordinationClient(home, start_router=False)
    updates = asyncio.Queue(maxsize=1)
    host = _StructuralHost()
    projector = CoordinationSnapshotProjector(
        host,
        RuntimeSessionStateCache("codex", host),
        {},
        CodexTimelineAccumulator(),
        CodexNoticeRegistry(),
    )

    def on_state(observed_thread_id, _stale_payload):
        if observed_thread_id != thread_id:
            return
        if updates.full():
            updates.get_nowait()
            updates.task_done()
        updates.put_nowait(None)

    peer = CoordinationPeer(client, host_id=host_id, on_state=on_state)
    last_authority = None
    projected = False

    def on_wire(envelope):
        params = envelope.get("params")
        if (
            envelope.get("method") != "thread-stream-state-changed"
            or not isinstance(params, dict)
            or params.get("conversationId") != thread_id
            or params.get("hostId") != host_id
        ):
            return
        change = params.get("change")
        if not isinstance(change, dict) or change.get("type") not in {
            "snapshot",
            "patches",
        }:
            return
        revision = change.get("revision")
        version = envelope.get("version")
        if type(revision) is not int or type(version) is not int:
            return
        fields = {
            "event": "wire",
            "kind": change["type"],
            "version": version,
            "revision": revision,
        }
        if type(change.get("baseRevision")) is int:
            fields["baseRevision"] = change["baseRevision"]
        _emit(**fields)

    remove_wire = client.add_broadcast_handler(on_wire)

    async def project():
        nonlocal last_authority, projected
        # Match the production facade: queued callback payload may predate a
        # later patch, so read current state and revision in the same event turn.
        state = peer.get_state(thread_id)
        if not isinstance(state, dict):
            return
        owner = peer.get_owner(thread_id)
        revision = peer.get_revision(thread_id)
        if owner is None or type(revision) is not int:
            return
        authority = (owner.client_id, revision)
        if authority == last_authority:
            return
        try:
            turns = enumerate_turns(state)
            complete = history_complete(state)
            item_count = sum(len(turn.get("items", [])) for turn in turns)
            request_count = len(state.get("requests", []))
            await projector.handle(
                "probe-session",
                thread_id,
                {
                    "threadId": thread_id,
                    "thread": state_to_native(state),
                    "canonicalComplete": complete,
                    "requests": state.get("requests", []),
                    "requestContexts": [],
                    "coordination": {
                        "role": "follower",
                        "ownerClientId": owner.client_id,
                        "revision": revision,
                    },
                    "presentation": presentation(state),
                    "capabilities": {"role": "follower", "goalControl": False},
                },
            )
        except Exception:  # noqa: BLE001 - private native data must never reach output
            _emit(event="error", code="projection-failed")
            return False
        last_authority = authority
        projected = True
        _emit(
            event="projection",
            threadId=thread_id,
            hostId=host_id,
            clientId=client.client_id,
            ownerClientId=owner.client_id,
            role="follower",
            revision=revision,
            turns=len(turns),
            items=item_count,
            pendingRequests=request_count,
            timelineItems=len(host.item_ids),
            status=host.status,
            complete=complete,
        )
        return True

    try:
        try:
            await asyncio.wait_for(peer.start(), connect_timeout)
        except TimeoutError:
            _emit(event="error", code="app-socket-unavailable")
            return 2
        except IpcError as exc:
            _emit(event="error", code=_error_code(exc))
            return 2
        except OSError:
            _emit(event="error", code="endpoint-inaccessible")
            return 2
        try:
            initial = await peer.follow(thread_id, timeout=connect_timeout)
        except (IpcError, TimeoutError) as exc:
            _emit(event="error", code=_error_code(exc))
            return 2
        except OSError:
            _emit(event="error", code="follow-unavailable")
            return 2
        if initial is None:
            _emit(event="error", code="no-owner")
            return 2
        if await project() is False:
            return 2
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration
        while (remaining := deadline - loop.time()) > 0:
            try:
                await asyncio.wait_for(updates.get(), remaining)
            except TimeoutError:
                break
            updates.task_done()
            if await project() is False:
                return 2
        if not projected:
            _emit(event="error", code="no-projected-snapshot")
            return 2
        return 0
    finally:
        remove_wire()
        try:
            await peer.unfollow(thread_id)
        finally:
            await peer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="Existing Codex Home")
    parser.add_argument("--thread-id", required=True, help="Existing native thread ID")
    parser.add_argument("--host-id", default="local", help="Coordination host ID")
    parser.add_argument(
        "--duration", type=float, default=2.0, help="Observation seconds (0..30)"
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=3.0,
        help="Connection/follow seconds (0, 10]",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run_probe(
            args.home.expanduser(),
            args.thread_id,
            host_id=args.host_id,
            duration=args.duration,
            connect_timeout=args.connect_timeout,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
