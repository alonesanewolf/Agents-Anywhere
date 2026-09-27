"""User-private Unix router election and authenticated peer routing."""

import asyncio
import errno
import os
import socket
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .wire import IpcError, read_frame, send_frame


async def open_stream(endpoint: Path):
    if sys.platform == "win32":
        raise IpcError("unsupported-platform")
    return await asyncio.open_unix_connection(str(endpoint))


async def close_stream(writer: asyncio.StreamWriter):
    """Discard a finished connection without waiting for a stalled peer to drain."""
    writer.transport.abort()
    try:
        await asyncio.wait_for(writer.wait_closed(), 0.1)
    except (OSError, TimeoutError):
        pass


async def listen_stream(callback, endpoint: Path):
    if sys.platform == "win32":
        raise IpcError("unsupported-platform")
    # Passing a path to asyncio.start_unix_server silently unlinks existing sockets.
    # Binding explicitly lets the kernel arbitrate election without destroying peers.
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    owned_info = None
    try:
        listener.bind(str(endpoint))
        owned_info = endpoint.lstat()
        os.chmod(endpoint, 0o600)
        # Listen before the first await so a competing election sees a live socket.
        listener.listen(100)
        listener.setblocking(False)
        return await asyncio.start_unix_server(callback, sock=listener)
    except BaseException:
        listener.close()
        if owned_info is not None and _same_socket(endpoint, owned_info):
            endpoint.unlink()
        raise


def validate_endpoint(endpoint: Path, *, create_parent: bool = False):
    if sys.platform == "win32" or not hasattr(os, "getuid"):
        raise IpcError("unsupported-platform")
    if create_parent:
        endpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        parent = endpoint.parent.lstat()
    except FileNotFoundError:
        raise IpcError("endpoint-not-found") from None
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o022
    ):
        raise IpcError("unsafe-endpoint-parent")
    try:
        info = endpoint.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise IpcError("unsafe-endpoint")
    return info


def _same_socket(endpoint: Path, expected) -> bool:
    try:
        current = endpoint.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == (
        expected.st_dev,
        expected.st_ino,
    )


async def _connectable(endpoint: Path) -> bool:
    try:
        _, writer = await asyncio.wait_for(open_stream(endpoint), 0.5)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
            return False
        raise IpcError("endpoint-connect-failed") from exc
    except TimeoutError as exc:
        # A busy or inaccessible listener is never evidence of a stale socket.
        raise IpcError("endpoint-connect-timeout") from exc
    await close_stream(writer)
    return True


async def elect_router(endpoint: Path, *, legacy_endpoint: Path | None = None):
    """Return (selected endpoint, owned router or None), never unlink a live peer."""
    info = validate_endpoint(endpoint, create_parent=True)
    if info is not None and await _connectable(endpoint):
        return endpoint, None
    if legacy_endpoint is not None:
        try:
            legacy_info = validate_endpoint(legacy_endpoint)
        except IpcError:
            legacy_info = None
        if legacy_info is not None and await _connectable(legacy_endpoint):
            return legacy_endpoint, None
    if info is not None:
        # Recheck both identity and liveness immediately before removal.
        validate_endpoint(endpoint)
        if await _connectable(endpoint):
            return endpoint, None
        if not _same_socket(endpoint, info):
            raise IpcError("endpoint-changed")
        endpoint.unlink()
    router = CoordinationRouter(endpoint)
    try:
        await router.start()
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        # Another election won. Do not remove its socket, even if not yet listening.
        for _ in range(10):
            validate_endpoint(endpoint)
            if await _connectable(endpoint):
                return endpoint, None
            await asyncio.sleep(0.02)
        raise IpcError("router-election-failed") from exc
    return endpoint, router


@dataclass(eq=False)
class _Peer:
    writer: asyncio.StreamWriter
    client_id: str | None = None
    client_type: str | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)


@dataclass
class _Pending:
    peer: _Peer
    future: asyncio.Future


class CoordinationRouter:
    def __init__(self, endpoint: Path):
        self.endpoint = endpoint
        self.server = None
        self._socket_info = None
        self._peers: set[_Peer] = set()
        self._sessions: set[asyncio.Task] = set()
        self._discoveries: dict[str, _Pending] = {}
        self._responses: dict[str, _Pending] = {}
        self._closed = False

    async def start(self):
        self.server = await listen_stream(self._accept, self.endpoint)
        self._socket_info = validate_endpoint(self.endpoint)
        os.chmod(self.endpoint, 0o600)

    def _accept(self, reader, writer):
        if self._closed:
            writer.transport.abort()
            return
        # Register before scheduling: close may cancel a task before its first step.
        peer = _Peer(writer)
        self._peers.add(peer)
        task = asyncio.create_task(self._session(reader, peer))
        self._sessions.add(task)
        task.add_done_callback(self._sessions.discard)

    async def _session(self, reader, peer):
        writer = peer.writer
        try:
            while not self._closed:
                message = await read_frame(reader)
                kind = message.get("type")
                if kind == "request":
                    if message.get("method") == "initialize":
                        await self._initialize(peer, message)
                    elif peer.client_id is not None:
                        task = asyncio.create_task(self._route(peer, message))
                        peer.tasks.add(task)
                        task.add_done_callback(peer.tasks.discard)
                    else:
                        await self._error(
                            peer, message.get("requestId"), "not-initialized"
                        )
                elif peer.client_id is not None and kind == "broadcast":
                    await self._broadcast(peer, message)
                elif peer.client_id is not None and kind in (
                    "response",
                    "client-discovery-response",
                ):
                    pending = (
                        self._responses if kind == "response" else self._discoveries
                    ).get(message.get("requestId"))
                    # Only the selected socket may satisfy this request/discovery.
                    if (
                        pending is not None
                        and pending.peer is peer
                        and not pending.future.done()
                    ):
                        if (
                            kind == "response"
                            and message.get("resultType") == "success"
                            and message.get("handledByClientId") != peer.client_id
                        ):
                            continue
                        pending.future.set_result(message)
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
            IpcError,
            TypeError,
        ):
            pass
        finally:
            self._peers.discard(peer)
            for pending in (*self._discoveries.values(), *self._responses.values()):
                if pending.peer is peer and not pending.future.done():
                    pending.future.set_exception(IpcError("client-disconnected"))
            for task in peer.tasks:
                task.cancel()
            await asyncio.gather(*peer.tasks, return_exceptions=True)
            await close_stream(writer)
            if peer.client_id and not self._closed:
                await self._status(peer, "disconnected")

    async def _initialize(self, peer, message):
        params = message.get("params")
        if (
            message.get("version", 0) != 0
            or not isinstance(params, dict)
            or not isinstance(params.get("clientType"), str)
        ):
            await self._error(peer, message.get("requestId"), "invalid-initialize")
            return
        if peer.client_id is None:
            peer.client_id = str(uuid.uuid4())
            peer.client_type = params["clientType"]
            await self._status(peer, "connected")
        await send_frame(
            peer.writer,
            {
                "type": "response",
                "requestId": message.get("requestId"),
                "resultType": "success",
                "method": "initialize",
                "handledByClientId": peer.client_id,
                "result": {"clientId": peer.client_id},
            },
        )

    async def _status(self, peer, status):
        await self._broadcast(
            peer,
            {
                "type": "broadcast",
                "sourceClientId": peer.client_id,
                "method": "client-status-changed",
                "version": 0,
                "params": {
                    "clientId": peer.client_id,
                    "clientType": peer.client_type,
                    "status": status,
                },
            },
        )

    async def _broadcast(self, source, message):
        targets = message.get("targetClientIds")
        if targets is not None and not isinstance(targets, list):
            return
        authenticated = {**message, "sourceClientId": source.client_id}
        for peer in tuple(self._peers):
            if (
                peer is source
                or peer.client_id is None
                or (targets is not None and peer.client_id not in targets)
            ):
                continue
            try:
                await send_frame(peer.writer, authenticated)
            except (OSError, ConnectionError, IpcError):
                peer.writer.close()

    async def _exchange(self, peer, message, collection, timeout):
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        collection[request_id] = _Pending(peer, future)
        try:
            async with asyncio.timeout(timeout):
                await send_frame(peer.writer, {**message, "requestId": request_id})
                return await future
        finally:
            collection.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A disconnect may fail the reply while send_frame is in drain().
                future.exception()

    async def _discover(self, peer, request):
        response = await self._exchange(
            peer,
            {"type": "client-discovery-request", "request": request},
            self._discoveries,
            10,
        )
        discovery_result = response.get("response")
        if (
            not isinstance(discovery_result, dict)
            or discovery_result.get("canHandle") is not True
        ):
            raise IpcError("client-cannot-handle-request")
        return peer

    async def _route(self, source, message):
        discovery_tasks = []
        request_id = message.get("requestId")
        try:
            if not isinstance(request_id, str) or not isinstance(
                message.get("method"), str
            ):
                raise IpcError("invalid-request")
            timeout_ms = message.get("timeoutMs", 10000)
            if (
                isinstance(timeout_ms, bool)
                or not isinstance(timeout_ms, (int, float))
                or not 0 < timeout_ms <= 86400000
            ):
                raise IpcError("invalid-timeout")
            request = {**message, "sourceClientId": source.client_id}
            targets = [
                peer
                for peer in self._peers
                if peer is not source
                and peer.client_id is not None
                and (
                    message.get("targetClientId") is None
                    or peer.client_id == message["targetClientId"]
                )
            ]
            discovery_tasks = [
                asyncio.create_task(self._discover(peer, request)) for peer in targets
            ]
            chosen = None
            for result in asyncio.as_completed(discovery_tasks):
                try:
                    chosen = await result
                    break
                except (IpcError, TimeoutError, OSError):
                    continue
            if chosen is None:
                raise IpcError("no-client-found")
            response = await self._exchange(
                chosen, request, self._responses, timeout_ms / 1000
            )
            if (
                response.get("resultType") == "success"
                and response.get("method") != message["method"]
            ):
                raise IpcError("invalid-response")
            await send_frame(source.writer, {**response, "requestId": request_id})
        except TimeoutError:
            await self._error(source, request_id, "request-timeout")
        except IpcError as exc:
            await self._error(source, request_id, exc.code)
        except (OSError, ConnectionError):
            await self._error(source, request_id, "client-disconnected")
        finally:
            for task in discovery_tasks:
                task.cancel()
            await asyncio.gather(*discovery_tasks, return_exceptions=True)

    async def _error(self, peer, request_id, code):
        try:
            await send_frame(
                peer.writer,
                {
                    "type": "response",
                    "requestId": request_id,
                    "resultType": "error",
                    "error": code,
                },
            )
        except (OSError, ConnectionError):
            pass

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self.server:
            self.server.close()
        for peer in tuple(self._peers):
            peer.writer.transport.abort()
        for task in tuple(self._sessions):
            task.cancel()
        await asyncio.gather(*tuple(self._sessions), return_exceptions=True)
        self._peers.clear()
        if self.server:
            await self.server.wait_closed()
        if self._socket_info is not None and _same_socket(
            self.endpoint, self._socket_info
        ):
            self.endpoint.unlink()
