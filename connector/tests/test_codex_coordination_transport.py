"""Independent wire fixtures and real Unix sockets for the coordination protocol."""

import asyncio
import json
import os
import socket
import struct
import tempfile
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

import pytest

from connector.runtimes.codex.coordination import CoordinationClient, IpcError
from connector.runtimes.codex.coordination.router import (
    CoordinationRouter,
    elect_router,
    listen_stream,
)
from connector.runtimes.codex.coordination.wire import (
    encode_frame,
    read_frame,
    request_version,
    request_version_matches,
)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(function(*args, **kwargs), 10))

    return run


@pytest.fixture
def tmp_path():
    # macOS sockaddr_un has a 104-byte limit, below pytest's default path length.
    with tempfile.TemporaryDirectory(prefix="aa-ipc-", dir="/tmp") as directory:
        yield Path(directory)


def frame(message):
    payload = json.dumps(message, ensure_ascii=False).encode()
    return struct.pack("<I", len(payload)) + payload


@asynccontextmanager
async def clients(tmp_path, count=2):
    peers = [
        CoordinationClient(tmp_path, endpoint=tmp_path / "ipc" / "ipc.sock")
        for _ in range(count)
    ]
    try:
        for peer in peers:
            await peer.start()
            await peer.wait_initialized()
        yield peers
    finally:
        for peer in reversed(peers):
            await peer.close()


def test_versions_match_remote_and_legacy_interrupt():
    assert request_version("thread-follower-start-turn", {}, None) == 2
    assert request_version("thread-follower-start-turn", {}, "remote") == 3
    assert request_version("thread-follower-interrupt-turn", {}, None) == 3
    assert (
        request_version("thread-follower-interrupt-turn", {"expectedTurnId": "t"}) == 4
    )
    assert request_version_matches("thread-follower-interrupt-turn", 3)
    assert not request_version_matches("thread-follower-start-turn", 1)
    assert request_version("future-method", {}) == 0


def test_encode_matches_literal_wire():
    assert encode_frame({"hello": "世界"}) == frame({"hello": "世界"})


@async_test
async def test_frames_fragmented_and_coalesced(tmp_path):
    endpoint = str(tmp_path / "frames.sock")
    messages = asyncio.Queue()

    async def consume(reader, writer):
        try:
            messages.put_nowait(await read_frame(reader))
            messages.put_nowait(await read_frame(reader))
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(consume, endpoint)
    try:
        _, writer = await asyncio.open_unix_connection(endpoint)
        payload = frame({"type": "broadcast", "params": "世"}) + frame({"tail": True})
        for chunk in (payload[:2], payload[2:7], payload[7:]):
            writer.write(chunk)
            await writer.drain()
        assert await asyncio.wait_for(messages.get(), 1) == {
            "type": "broadcast",
            "params": "世",
        }
        assert await asyncio.wait_for(messages.get(), 1) == {"tail": True}
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "payload",
    [
        struct.pack("<I", 268435457),
        struct.pack("<I", 0),
        frame([]),
        b"\x01\x00\x00\x00\xff",
    ],
)
@async_test
async def test_rejects_invalid_frame_without_waiting_for_body(payload):
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    with pytest.raises(IpcError):
        await asyncio.wait_for(read_frame(reader), 0.1)


@async_test
async def test_discovery_targeted_and_out_of_order_full_responses(tmp_path):
    async with clients(tmp_path, 3) as (source, owner, other):
        observations = []

        async def predicate(envelope):
            observations.append(envelope)
            return envelope["params"]["accept"]

        async def handle(envelope):
            await asyncio.sleep(envelope["params"]["delay"])
            return {"echo": envelope["params"]["value"], "unknown": [1, 2]}

        owner.add_request_handler("echo", predicate, handle)
        other.add_request_handler("echo", lambda envelope: False, handle)
        first, second = await asyncio.gather(
            source.request("echo", {"value": "slow", "delay": 0.05, "accept": True}),
            source.request(
                "echo",
                {"value": "fast", "delay": 0, "accept": True},
                target_client_id=owner.client_id,
            ),
        )
        assert first["result"] == {"echo": "slow", "unknown": [1, 2]}
        assert second["handledByClientId"] == owner.client_id
        assert second["method"] == "echo"
        assert first["requestId"] != second["requestId"]
        assert all(item["sourceClientId"] == source.client_id for item in observations)
        with pytest.raises(IpcError, match="no-client-found"):
            await source.request(
                "echo", {"accept": False}, target_client_id=owner.client_id
            )


@async_test
async def test_broadcast_sender_exclusion_targets_and_remove(tmp_path):
    async with clients(tmp_path, 3) as (source, target, other):
        events = [asyncio.Queue() for _ in range(3)]

        def receive(queue):
            async def handler(envelope):
                if envelope["method"] == "example":
                    await queue.put(envelope)

            return handler

        removes = [
            peer.add_broadcast_handler(receive(queue))
            for peer, queue in zip((source, target, other), events)
        ]
        await source.broadcast(
            "example", {"future": {"x": 1}}, target_client_ids=[target.client_id]
        )
        event = await asyncio.wait_for(events[1].get(), 1)
        assert event["sourceClientId"] == source.client_id
        assert event["params"] == {"future": {"x": 1}}
        assert events[0].empty() and events[2].empty()
        removes[1]()
        await source.broadcast("example", {}, target_client_ids=[])
        await source.broadcast("example", {})
        await asyncio.wait_for(events[2].get(), 1)
        assert events[1].empty()


@async_test
async def test_timeout_does_not_retry_and_close_fails_pending(tmp_path):
    async with clients(tmp_path) as (source, owner):
        entered = asyncio.Event()
        calls = []

        async def handle(envelope):
            calls.append(envelope)
            entered.set()
            await asyncio.Event().wait()

        owner.add_request_handler("hang", lambda _: True, handle)
        with pytest.raises(IpcError, match="timeout"):
            await source.request("hang", {}, timeout=0.05)
        assert len(calls) == 1
        entered.clear()
        pending = asyncio.create_task(source.request("hang", {}, timeout=5))
        await asyncio.wait_for(entered.wait(), 1)
        await owner.close()
        with pytest.raises(IpcError, match="client-disconnected"):
            await pending


@async_test
async def test_unsafe_endpoint_regular_file_and_symlink_preserved(tmp_path):
    parent = tmp_path / "ipc"
    parent.mkdir(mode=0o700)
    endpoint = parent / "ipc.sock"
    endpoint.write_text("keep")
    peer = CoordinationClient(tmp_path)
    with pytest.raises(IpcError, match="unsafe-endpoint"):
        await peer.start()
    assert endpoint.read_text() == "keep"
    endpoint.unlink()
    endpoint.symlink_to(tmp_path / "missing")
    with pytest.raises(IpcError, match="unsafe-endpoint"):
        await CoordinationClient(tmp_path).start()
    assert endpoint.is_symlink()


@async_test
async def test_close_rejects_initialization_waiter_and_removes_owned_socket(tmp_path):
    peer = CoordinationClient(tmp_path)
    waiting = asyncio.create_task(peer.wait_initialized())
    await asyncio.sleep(0)
    await peer.close()
    with pytest.raises(IpcError, match="disposed"):
        await waiting
    async with clients(tmp_path, 1):
        assert (tmp_path / "ipc" / "ipc.sock").exists()
        assert os.stat(tmp_path / "ipc" / "ipc.sock").st_mode & 0o777 == 0o600
    assert not (tmp_path / "ipc" / "ipc.sock").exists()


@async_test
async def test_broadcast_order_with_nested_request_and_slow_first_callback(tmp_path):
    async with clients(tmp_path) as (source, target):
        completed = asyncio.Queue()
        source.add_request_handler("nested", lambda _: True, lambda _: {"ok": True})

        async def handle(envelope):
            if envelope["method"] != "ordered":
                return
            if envelope["params"]["revision"] == 1:
                await asyncio.sleep(0.03)
                reply = await target.request(
                    "nested", {}, target_client_id=source.client_id
                )
                assert reply["result"] == {"ok": True}
            completed.put_nowait(envelope["params"]["revision"])

        target.add_broadcast_handler(handle)
        await source.broadcast("ordered", {"revision": 1})
        await source.broadcast("ordered", {"revision": 2})
        assert await asyncio.wait_for(completed.get(), 1) == 1
        assert await asyncio.wait_for(completed.get(), 1) == 2


@async_test
async def test_listener_does_not_unlink_existing_active_socket(tmp_path):
    endpoint = tmp_path / "active.sock"
    first = await asyncio.start_unix_server(
        lambda reader, writer: writer.close(), str(endpoint)
    )
    original = endpoint.lstat().st_ino
    second = None
    try:
        with pytest.raises(OSError):
            second = await listen_stream(
                lambda reader, writer: writer.close(), endpoint
            )
        assert endpoint.lstat().st_ino == original
    finally:
        if second is not None:
            second.close()
            await second.wait_closed()
        first.close()
        await first.wait_closed()


@async_test
async def test_concurrent_election_and_stale_owned_socket(tmp_path):
    endpoint = tmp_path / "ipc.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(endpoint))
    stale.close()
    os.chmod(endpoint, 0o600)
    results = await asyncio.gather(elect_router(endpoint), elect_router(endpoint))
    owned = [router for _, router in results if router is not None]
    try:
        assert len(owned) == 1
        reader, writer = await asyncio.open_unix_connection(str(endpoint))
        writer.write(
            frame(
                {
                    "type": "request",
                    "requestId": "init",
                    "sourceClientId": "initializing-client",
                    "version": 0,
                    "method": "initialize",
                    "params": {"clientType": "raw"},
                }
            )
        )
        await writer.drain()
        response = await asyncio.wait_for(read_frame(reader), 1)
        assert response["resultType"] == "success"
        writer.close()
        await writer.wait_closed()
    finally:
        for router in owned:
            await router.close()


@asynccontextmanager
async def raw_peer(endpoint):
    reader, writer = await asyncio.open_unix_connection(str(endpoint))
    writer.write(
        frame(
            {
                "type": "request",
                "requestId": "raw-init",
                "sourceClientId": "initializing-client",
                "version": 0,
                "method": "initialize",
                "params": {"clientType": "raw"},
            }
        )
    )
    await writer.drain()
    response = await asyncio.wait_for(read_frame(reader), 1)
    try:
        yield reader, writer, response["result"]["clientId"]
    finally:
        writer.close()
        await writer.wait_closed()


async def read_kind(reader, kind):
    while True:
        message = await asyncio.wait_for(read_frame(reader), 1)
        if message["type"] == kind:
            return message


@async_test
async def test_raw_source_authenticated_and_unsupported_version_declined(tmp_path):
    async with clients(tmp_path) as (source, owner):
        received = asyncio.Queue()
        owner.add_broadcast_handler(received.put)
        owner.add_request_handler(
            "thread-follower-start-turn", lambda _: True, lambda _: {}
        )
        async with raw_peer(source.endpoint) as (reader, writer, raw_id):
            writer.write(
                frame(
                    {
                        "type": "broadcast",
                        "sourceClientId": owner.client_id,
                        "method": "spoof",
                        "version": 0,
                        "params": {},
                    }
                )
            )
            writer.write(
                frame(
                    {
                        "type": "request",
                        "requestId": "wrong-version",
                        "sourceClientId": owner.client_id,
                        "method": "thread-follower-start-turn",
                        "version": 99,
                        "params": {},
                        "targetClientId": owner.client_id,
                    }
                )
            )
            await writer.drain()
            while True:
                broadcast = await asyncio.wait_for(received.get(), 1)
                if broadcast["method"] == "spoof":
                    break
            assert broadcast["sourceClientId"] == raw_id
            response = await read_kind(reader, "response")
            assert response == {
                "type": "response",
                "requestId": "wrong-version",
                "resultType": "error",
                "error": "no-client-found",
            }


@async_test
async def test_dishonest_discovery_and_response_cannot_satisfy_other_peer(tmp_path):
    async with clients(tmp_path, 1) as (source,):  # noqa: SIM117 - peer setup depends on the selected endpoint
        async with raw_peer(source.endpoint) as (owner_reader, owner_writer, owner_id):
            async with raw_peer(source.endpoint) as (_, attacker_writer, attacker_id):
                pending = asyncio.create_task(
                    source.request("raw-echo", {}, target_client_id=owner_id, timeout=1)
                )
                discovery = await read_kind(owner_reader, "client-discovery-request")
                attacker_writer.write(
                    frame(
                        {
                            "type": "client-discovery-response",
                            "requestId": discovery["requestId"],
                            "response": {"canHandle": True},
                        }
                    )
                )
                await attacker_writer.drain()
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(read_frame(owner_reader), 0.03)
                owner_writer.write(
                    frame(
                        {
                            "type": "client-discovery-response",
                            "requestId": discovery["requestId"],
                            "response": {"canHandle": True},
                        }
                    )
                )
                await owner_writer.drain()
                request = await read_kind(owner_reader, "request")
                success = {
                    "type": "response",
                    "requestId": request["requestId"],
                    "resultType": "success",
                    "method": "raw-echo",
                    "handledByClientId": owner_id,
                    "result": {"honest": True},
                }
                attacker_writer.write(frame({**success, "result": {"honest": False}}))
                owner_writer.write(
                    frame(
                        {
                            **success,
                            "handledByClientId": attacker_id,
                            "result": {"honest": False},
                        }
                    )
                )
                await attacker_writer.drain()
                await owner_writer.drain()
                await asyncio.sleep(0.03)
                assert not pending.done()
                owner_writer.write(frame(success))
                await owner_writer.drain()
                assert (await pending)["result"] == {"honest": True}


@async_test
async def test_reconnect_reset_new_id_status_and_pending_failure(tmp_path):
    endpoint = tmp_path / "ipc.sock"
    router = CoordinationRouter(endpoint)
    await router.start()
    peer = CoordinationClient(tmp_path, endpoint=endpoint, start_router=False)
    events = asyncio.Queue()
    peer.add_broadcast_handler(events.put)
    try:
        await peer.start()
        await peer.wait_initialized()
        old_id = peer.client_id
        await events.get()  # Synthetic self connected.
        async with raw_peer(endpoint) as (reader, writer, owner_id):
            pending = asyncio.create_task(
                peer.request("hang", {}, target_client_id=owner_id)
            )
            discovery = await read_kind(reader, "client-discovery-request")
            writer.write(
                frame(
                    {
                        "type": "client-discovery-response",
                        "requestId": discovery["requestId"],
                        "response": {"canHandle": True},
                    }
                )
            )
            await writer.drain()
            await read_kind(reader, "request")
            await router.close()
            with pytest.raises(IpcError, match="connection-closed"):
                await pending
        while True:
            reset = await asyncio.wait_for(events.get(), 1)
            if reset["method"] == "ipc-connection-reset":
                break
        assert reset["sourceClientId"] == old_id
        router = CoordinationRouter(endpoint)
        await router.start()
        await peer.wait_initialized(timeout=3)
        assert peer.client_id != old_id
        connected = await asyncio.wait_for(events.get(), 1)
        assert connected["params"]["status"] == "connected"
    finally:
        await peer.close()
        await router.close()
    assert not endpoint.exists()


@async_test
async def test_insecure_parent_symlink_parent_and_unsupported_platform(
    tmp_path, monkeypatch
):
    import connector.runtimes.codex.coordination.router as router_module

    parent = tmp_path / "ipc"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o777)
    with pytest.raises(IpcError, match="unsafe-endpoint-parent"):
        await CoordinationClient(tmp_path, endpoint=parent / "ipc.sock").start()
    os.chmod(parent, 0o700)
    link = tmp_path / "link"
    link.symlink_to(parent)
    with pytest.raises(IpcError, match="unsafe-endpoint-parent"):
        await CoordinationClient(tmp_path, endpoint=link / "ipc.sock").start()
    monkeypatch.setattr(router_module.sys, "platform", "win32")
    with pytest.raises(IpcError, match="unsupported-platform"):
        await CoordinationClient(tmp_path).start()


@async_test
async def test_router_close_preserves_successor_socket_and_close_cancels_handlers(
    tmp_path,
):
    async with clients(tmp_path) as (source, owner):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def forever(envelope):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        owner.add_broadcast_handler(forever)
        await source.broadcast("hang-broadcast", {})
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(owner.close(), 1)
        assert cancelled.is_set()
    endpoint = tmp_path / "replacement.sock"
    router = CoordinationRouter(endpoint)
    await router.start()
    endpoint.unlink()
    successor = socket.socket(socket.AF_UNIX)
    successor.bind(str(endpoint))
    try:
        successor_inode = endpoint.lstat().st_ino
        await router.close()
        assert endpoint.lstat().st_ino == successor_inode
    finally:
        successor.close()


@async_test
async def test_legacy_secure_endpoint_fallback_and_active_listener_not_owned(tmp_path):
    legacy_parent = tmp_path / "legacy"
    legacy_parent.mkdir(mode=0o700)
    legacy = legacy_parent / "old.sock"
    router = CoordinationRouter(legacy)
    await router.start()
    try:
        selected, owner = await elect_router(
            tmp_path / "ipc" / "ipc.sock", legacy_endpoint=legacy
        )
        assert selected == legacy and owner is None
        selected, owner = await elect_router(legacy)
        assert selected == legacy and owner is None
    finally:
        await router.close()


@async_test
async def test_predicate_failure_declines_and_request_handler_error_is_explicit(
    tmp_path,
):
    async with clients(tmp_path) as (source, owner):

        def fail(envelope):
            raise RuntimeError("private request contents must not reach wire")

        owner.add_request_handler("decline", fail, lambda _: {})
        owner.add_request_handler("failure", lambda _: True, fail)
        with pytest.raises(IpcError, match="no-client-found"):
            await source.request("decline", {}, target_client_id=owner.client_id)
        with pytest.raises(IpcError, match="error-handling-request"):
            await source.request("failure", {}, target_client_id=owner.client_id)
        owner.add_request_handler(
            "typed-error",
            lambda _: True,
            lambda _: (_ for _ in ()).throw(IpcError("unsupported-operation")),
        )
        with pytest.raises(IpcError) as error:
            await source.request("typed-error", {}, target_client_id=owner.client_id)
        assert error.value.code == "unsupported-operation"


@async_test
async def test_malformed_discovery_response_declines_without_stranding_request(
    tmp_path,
):
    async with clients(tmp_path, 1) as (source,):  # noqa: SIM117 - raw connection depends on selected endpoint
        async with raw_peer(source.endpoint) as (reader, writer, raw_id):
            pending = asyncio.create_task(
                source.request("malformed", {}, target_client_id=raw_id, timeout=0.5)
            )
            discovery = await read_kind(reader, "client-discovery-request")
            writer.write(
                frame(
                    {
                        "type": "client-discovery-response",
                        "requestId": discovery["requestId"],
                        "response": [],
                    }
                )
            )
            await writer.drain()
            with pytest.raises(IpcError, match="no-client-found"):
                await pending


@pytest.mark.parametrize(
    "result",
    [
        {"resultType": "future"},
        {"resultType": "success", "method": "wrong"},
        {"resultType": "success", "handledByClientId": "dishonest"},
    ],
)
@async_test
async def test_client_rejects_non_success_or_mismatched_native_response(
    tmp_path, result
):
    endpoint = tmp_path / "native.sock"
    tasks = []

    async def native(reader, writer):
        try:
            initialize = await read_frame(reader)
            writer.write(
                frame(
                    {
                        "type": "response",
                        "requestId": initialize["requestId"],
                        "resultType": "success",
                        "method": "initialize",
                        "handledByClientId": "self",
                        "result": {"clientId": "self"},
                    }
                )
            )
            await writer.drain()
            request = await read_frame(reader)
            writer.write(
                frame(
                    {
                        "type": "response",
                        "requestId": request["requestId"],
                        "resultType": "success",
                        "method": "echo",
                        "handledByClientId": "owner",
                        "result": {},
                        **result,
                    }
                )
            )
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader, writer):
        tasks.append(asyncio.create_task(native(reader, writer)))

    server = await listen_stream(accept, endpoint)
    peer = CoordinationClient(tmp_path, endpoint=endpoint, start_router=False)
    try:
        await peer.start()
        await peer.wait_initialized()
        with pytest.raises(IpcError, match="invalid-response"):
            await peer.request("echo", {}, target_client_id="owner")
    finally:
        await peer.close()
        server.close()
        await asyncio.gather(*tasks)
        await server.wait_closed()


@async_test
async def test_concurrent_client_start_initializes_once_and_all_tasks_stop(tmp_path):
    peer = CoordinationClient(tmp_path, endpoint=tmp_path / "ipc.sock")
    events = asyncio.Queue()
    peer.add_broadcast_handler(events.put)
    original_tasks = asyncio.all_tasks()
    try:
        await asyncio.gather(peer.start(), peer.start())
        await peer.wait_initialized()
        connected = await asyncio.wait_for(events.get(), 1)
        assert connected["params"]["status"] == "connected"
        await asyncio.sleep(0.03)
        assert events.empty()
    finally:
        await peer.close()
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - original_tasks)
