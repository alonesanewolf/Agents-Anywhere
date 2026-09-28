"""Read-only probe behavior against the production coordination peer and AA projector."""

import asyncio
import json
import tempfile
from pathlib import Path

from connector.runtimes.codex.coordination import transport
from connector.runtimes.codex.coordination.peer import CoordinationPeer
from connector.runtimes.codex.coordination.projection import native_to_state
from connector.runtimes.codex.coordination.router import CoordinationRouter
from connector.runtimes.codex.coordination.transport import CoordinationClient
from scripts.probe_codex_coordination import main, run_probe

PRIVATE = "PRIVATE_NEVER_PRINT_731ae"


def _lines(capsys):
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err
    return [json.loads(line) for line in captured.out.splitlines()], captured.err


def _home():
    return tempfile.TemporaryDirectory(prefix="aa-probe-", dir="/tmp")


async def _owner(home, state):
    endpoint = home / "ipc" / "ipc.sock"
    endpoint.parent.mkdir(mode=0o700)
    router = CoordinationRouter(endpoint)
    await router.start()
    mutations = []

    async def handle(method, params):
        mutations.append(method)
        raise AssertionError("probe sent an owner operation")

    owner = CoordinationPeer(
        CoordinationClient(home, start_router=False), owner_handler=handle
    )
    await owner.start()
    await owner.claim("thread-test", state)
    return router, owner, mutations


def test_probe_projects_snapshot_and_patch_without_private_output_or_owner_operations(
    monkeypatch, capsys
):
    async def forbidden(*args, **kwargs):
        raise AssertionError("probe elected a router")

    monkeypatch.setattr(transport, "elect_router", forbidden)

    async def run(home):
        raw = {
            "id": "thread-test",
            "title": PRIVATE,
            "cwd": f"/secret/{PRIVATE}",
            "threadGoal": {"objective": PRIVATE},
            "requests": [
                {
                    "id": 7,
                    "method": "item/tool/requestUserInput",
                    "params": {"message": PRIVATE},
                }
            ],
            "turns": [
                {
                    "id": "turn-a",
                    "status": "completed",
                    "items": [
                        {
                            "id": "u",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": PRIVATE}],
                        },
                        {"id": "a", "type": "agentMessage", "text": PRIVATE},
                    ],
                }
            ],
        }
        router, owner, mutations = await _owner(
            home, native_to_state(raw, complete=True)
        )
        try:

            async def publish():
                for _ in range(100):
                    if owner._owned["thread-test"].followers:
                        break
                    await asyncio.sleep(0.005)
                await asyncio.sleep(0.025)
                await owner.publish_patches(
                    "thread-test",
                    [
                        {
                            "op": "replace",
                            "path": ["turns", 0, "items", 1, "text"],
                            "value": PRIVATE + "2",
                        }
                    ],
                )

            publisher = asyncio.create_task(publish())
            try:
                result = await run_probe(
                    home, "thread-test", duration=0.2, connect_timeout=1
                )
                await publisher
            finally:
                publisher.cancel()
                await asyncio.gather(publisher, return_exceptions=True)
            assert result == 0
            assert not mutations
            assert not owner._owned["thread-test"].followers
        finally:
            await owner.close()
            await router.close()

    with _home() as directory:
        asyncio.run(run(Path(directory)))
    lines, errors = _lines(capsys)
    assert errors == ""
    assert any(
        row.get("event") == "wire"
        and row.get("kind") == "snapshot"
        and row.get("version") == 11
        for row in lines
    )
    assert any(
        row.get("event") == "wire"
        and row.get("kind") == "patches"
        and row.get("version") == 11
        for row in lines
    )
    projected = [row for row in lines if row.get("event") == "projection"]
    snapshot_revisions = {
        row["revision"]
        for row in lines
        if row.get("event") == "wire" and row.get("kind") == "snapshot"
    }
    patch_revisions = {
        row["revision"]
        for row in lines
        if row.get("event") == "wire" and row.get("kind") == "patches"
    }
    assert patch_revisions and max(patch_revisions) > min(snapshot_revisions)
    projected_revisions = {row["revision"] for row in projected}
    assert snapshot_revisions & projected_revisions
    assert patch_revisions <= projected_revisions
    assert projected and projected[-1]["timelineItems"] == 2
    assert projected[-1]["turns"] == 1 and projected[-1]["pendingRequests"] == 1
    assert projected[-1]["status"] == "waiting_approval"
    assert set().union(*(row.keys() for row in lines)) <= {
        "event",
        "code",
        "threadId",
        "clientId",
        "ownerClientId",
        "hostId",
        "role",
        "revision",
        "baseRevision",
        "kind",
        "version",
        "turns",
        "items",
        "pendingRequests",
        "timelineItems",
        "status",
        "complete",
    }


def test_missing_socket_exits_bounded_without_election_or_socket_creation(
    monkeypatch, capsys
):
    async def forbidden(*args, **kwargs):
        raise AssertionError("probe elected a router")

    monkeypatch.setattr(transport, "elect_router", forbidden)
    with _home() as directory:
        home = Path(directory)
        (home / "ipc").mkdir(mode=0o700)
        assert (
            asyncio.run(
                run_probe(home, "thread-test", duration=0.01, connect_timeout=0.1)
            )
            == 2
        )
        assert not (home / "ipc" / "ipc.sock").exists()
    lines, errors = _lines(capsys)
    assert lines == [{"event": "error", "code": "app-socket-unavailable"}]
    assert errors == ""


def test_inaccessible_endpoint_parent_returns_sanitized_bounded_error(
    monkeypatch, capsys
):
    with _home() as directory:
        home = Path(directory)
        inaccessible = home / "ipc"
        inaccessible.mkdir(mode=0o700)
        original_lstat = Path.lstat

        def denied(path):
            if path == inaccessible:
                raise PermissionError(f"access denied: {inaccessible}/{PRIVATE}")
            return original_lstat(path)

        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "lstat", denied)
            result = main(
                [
                    "--home",
                    str(home),
                    "--thread-id",
                    "thread-test",
                    "--duration",
                    "0.01",
                    "--connect-timeout",
                    "0.1",
                ]
            )
        assert result == 2
        assert not (inaccessible / "ipc.sock").exists()
    lines, errors = _lines(capsys)
    assert lines == [{"event": "error", "code": "endpoint-inaccessible"}]
    assert errors == ""


def test_missing_owner_returns_actionable_error_and_closes_peer(capsys):
    async def run(home):
        endpoint = home / "ipc" / "ipc.sock"
        endpoint.parent.mkdir(mode=0o700)
        router = CoordinationRouter(endpoint)
        await router.start()
        try:
            assert (
                await run_probe(home, "thread-test", duration=0.01, connect_timeout=0.5)
                == 2
            )
        finally:
            await router.close()

    with _home() as directory:
        asyncio.run(run(Path(directory)))
    lines, errors = _lines(capsys)
    assert lines[-1] == {"event": "error", "code": "no-owner"}
    assert errors == ""


def test_projection_failure_does_not_leak_private_state_or_leave_subscription(capsys):
    async def run(home):
        router, owner, mutations = await _owner(
            home, {"id": "thread-test", "turns": PRIVATE}
        )
        try:
            assert (
                await run_probe(home, "thread-test", duration=0.01, connect_timeout=0.5)
                == 2
            )
            assert not owner._owned["thread-test"].followers
            assert not mutations
        finally:
            await owner.close()
            await router.close()

    with _home() as directory:
        asyncio.run(run(Path(directory)))
    lines, errors = _lines(capsys)
    assert lines[-1] == {"event": "error", "code": "projection-failed"}
    assert errors == ""
