"""Async headless peer for the installed Codex IDE coordination protocol."""

import asyncio
import inspect
import os
import uuid
from pathlib import Path

from farfield_python import ProtocolValidationError, parse_modern_ipc_frame
from loguru import logger

from .router import close_stream, elect_router, open_stream, validate_endpoint
from .wire import (
    INITIALIZING_CLIENT,
    IpcError,
    method_version,
    read_frame,
    request_version,
    request_version_matches,
    send_frame,
)


async def _invoke(handler, envelope):
    result = handler(envelope)
    return await result if inspect.isawaitable(result) else result


class CoordinationClient:
    def __init__(
        self,
        codex_home,
        *,
        endpoint=None,
        client_type="agents-anywhere",
        start_router=True,
    ):
        self.endpoint = (
            Path(endpoint)
            if endpoint is not None
            else Path(codex_home) / "ipc" / "ipc.sock"
        )
        self.client_type = client_type
        self.start_router = start_router
        self._use_legacy = endpoint is None
        self._client_id = INITIALIZING_CLIENT
        self._writer = None
        self._router = None
        self._runner = None
        self._closed = False
        self._handlers = {}
        self._broadcast_handlers = set()
        self._pending: dict[str, asyncio.Future] = {}
        self._waiters: set[asyncio.Future] = set()
        self._tasks: set[asyncio.Task] = set()
        self._broadcast_queue = asyncio.Queue()
        self._broadcast_task = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def client_id(self):
        return self._client_id

    async def start(self):
        async with self._lifecycle_lock:
            await self._start()

    async def _start(self):
        if self._closed:
            raise IpcError("disposed")
        if self._runner is not None:
            return
        # Validate synchronously so unsafe paths are explicit startup errors.
        if self.start_router:
            self.endpoint, self._router = await elect_router(
                self.endpoint,
                legacy_endpoint=Path(f"/tmp/codex-ipc/ipc-{os.getuid()}.sock")
                if self._use_legacy and hasattr(os, "getuid")
                else None,
            )
        else:
            validate_endpoint(self.endpoint)
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        self._runner = asyncio.create_task(self._run())

    async def wait_initialized(self, timeout=5):
        if self._closed:
            raise IpcError("disposed")
        if self._client_id != INITIALIZING_CLIENT:
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except TimeoutError as exc:
            raise IpcError("initialization-timeout") from exc
        finally:
            self._waiters.discard(waiter)

    def add_request_handler(self, method, can_handle, handler):
        registration = (can_handle, handler)
        self._handlers[method] = registration

        def remove():
            if self._handlers.get(method) is registration:
                self._handlers.pop(method)

        return remove

    def add_broadcast_handler(self, handler):
        self._broadcast_handlers.add(handler)
        return lambda: self._broadcast_handlers.discard(handler)

    async def request(
        self, method, params, *, target_client_id=None, host_id=None, timeout=5
    ):
        self._require_connection(method)
        request_id = str(uuid.uuid4())
        message = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self._client_id,
            "version": request_version(method, params, host_id),
            "method": method,
            "params": params,
            "timeoutMs": timeout * 1000,
        }
        if target_client_id is not None:
            message["targetClientId"] = target_client_id
        if host_id is not None:
            message["hostId"] = host_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                await send_frame(self._writer, message)
                response = await future
            if response.get("resultType") == "error":
                code = response.get("error")
                if not isinstance(code, str):
                    raise IpcError("invalid-response")
                raise IpcError(code)
            if (
                response.get("resultType") != "success"
                or response.get("method") != method
                or not isinstance(response.get("handledByClientId"), str)
                or "result" not in response
                or (
                    target_client_id is not None
                    and response.get("handledByClientId") != target_client_id
                )
            ):
                raise IpcError("invalid-response")
            return response
        except TimeoutError as exc:
            # Never resend: the selected owner may already have executed it.
            raise IpcError("timeout") from exc
        except (ConnectionError, OSError) as exc:
            raise IpcError("disposed" if self._closed else "connection-closed") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Disposal can reject the reply while the sender is still in drain().
                future.exception()

    async def broadcast(self, method, params, *, target_client_ids=None):
        self._require_connection(method)
        if target_client_ids == []:
            return
        message = {
            "type": "broadcast",
            "sourceClientId": self._client_id,
            "method": method,
            "version": method_version(method),
            "params": params,
        }
        if target_client_ids is not None:
            message["targetClientIds"] = target_client_ids
        try:
            await send_frame(self._writer, message)
        except (OSError, ConnectionError) as exc:
            raise IpcError("connection-closed") from exc

    def _require_connection(self, method):
        if self._closed:
            raise IpcError("disposed")
        if self._writer is None or self._writer.is_closing():
            raise IpcError("not-connected")
        if self._client_id == INITIALIZING_CLIENT and method != "initialize":
            raise IpcError("not-initialized")

    def _spawn(self, awaitable):
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self):
        while not self._closed:
            initialize_task = None
            try:
                validate_endpoint(self.endpoint)
                reader, self._writer = await open_stream(self.endpoint)
                initialize_task = asyncio.create_task(self._initialize())
                while not self._closed:
                    message = await read_frame(reader)
                    # The SDK validates complete Desktop coordination events at
                    # the socket boundary. Responses retain AA's own validation
                    # below so a malformed reply fails its request without
                    # tearing down unrelated in-flight work.
                    if message.get("type") in {
                        "broadcast",
                        "request",
                        "client-discovery-request",
                    }:
                        try:
                            parse_modern_ipc_frame(message)
                        except ProtocolValidationError as exc:
                            raise IpcError("invalid-envelope") from exc
                    if message.get("type") == "response":
                        if not isinstance(message.get("requestId"), str):
                            raise IpcError("invalid-envelope")
                        future = self._pending.get(message.get("requestId"))
                        if future is not None and not future.done():
                            future.set_result(message)
                            if (
                                self._client_id == INITIALIZING_CLIENT
                                and message.get("method") == "initialize"
                                and initialize_task is not None
                            ):
                                # Initialization validates/assigns the id and queues
                                # self status before a coalesced targeted frame is read.
                                await initialize_task
                    elif message.get("type") == "broadcast":
                        targets = message.get("targetClientIds")
                        if targets is not None and not isinstance(targets, list):
                            raise IpcError("invalid-envelope")
                        if targets is None or self._client_id in targets:
                            self._broadcast_queue.put_nowait(message)
                    else:
                        self._spawn(self._dispatch(message, self._writer))
            except (OSError, ConnectionError, asyncio.IncompleteReadError, IpcError):
                pass
            finally:
                if initialize_task:
                    initialize_task.cancel()
                    await asyncio.gather(initialize_task, return_exceptions=True)
                writer, self._writer = self._writer, None
                if writer:
                    await close_stream(writer)
                for task in self._tasks:
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                self._fail_pending("disposed" if self._closed else "connection-closed")
                if self._client_id != INITIALIZING_CLIENT:
                    self._broadcast_queue.put_nowait(
                        {
                            "type": "broadcast",
                            "sourceClientId": self._client_id,
                            "method": "ipc-connection-reset",
                            "version": 1,
                            "params": {},
                        }
                    )
                self._client_id = INITIALIZING_CLIENT
            if not self._closed:
                await asyncio.sleep(1)
                if self.start_router:
                    try:
                        selected, router = await elect_router(self.endpoint)
                        self.endpoint = selected
                        if router is not None:
                            self._router = router
                    except (OSError, IpcError):
                        pass

    async def _initialize(self):
        try:
            response = await self.request(
                "initialize", {"clientType": self.client_type}
            )
            client_id = response.get("result", {}).get("clientId")
            if (
                not isinstance(client_id, str)
                or not client_id
                or client_id == INITIALIZING_CLIENT
            ):
                raise IpcError("invalid-initialize-response")
            self._client_id = client_id
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.set_result(None)
            self._broadcast_queue.put_nowait(
                {
                    "type": "broadcast",
                    "sourceClientId": client_id,
                    "method": "client-status-changed",
                    "version": 0,
                    "params": {
                        "clientId": client_id,
                        "clientType": self.client_type,
                        "status": "connected",
                    },
                }
            )
        except (IpcError, TypeError, AttributeError):
            if self._writer:
                self._writer.close()

    async def _emit(self, message):
        for handler in tuple(self._broadcast_handlers):
            try:
                await _invoke(handler, message)
            except Exception:  # noqa: BLE001 - isolate arbitrary application callbacks
                # Handler failure must not strand the socket or leak private text.
                logger.warning("Codex IPC broadcast handler failed")

    async def _broadcast_loop(self):
        # Preserve revision order without blocking response demultiplexing or requests.
        while True:
            message = await self._broadcast_queue.get()
            try:
                await self._emit(message)
            finally:
                self._broadcast_queue.task_done()

    async def _dispatch(self, message, writer):
        kind = message.get("type")
        try:
            if kind == "broadcast":
                targets = message.get("targetClientIds")
                if targets is None or self._client_id in targets:
                    await self._emit(message)
                return
            request = (
                message.get("request")
                if kind == "client-discovery-request"
                else message
            )
            if not isinstance(request, dict):
                return
            method = request.get("method")
            registration = self._handlers.get(method)
            compatible = isinstance(method, str) and request_version_matches(
                method, request.get("version", 0), request.get("hostId")
            )
            if kind == "client-discovery-request":
                accepted = (
                    compatible
                    and registration is not None
                    and bool(await _invoke(registration[0], request))
                )
                await send_frame(
                    writer,
                    {
                        "type": "client-discovery-response",
                        "requestId": message.get("requestId"),
                        "response": {"canHandle": accepted},
                    },
                )
            elif kind == "request":
                if not compatible:
                    raise IpcError("request-version-mismatch")
                if registration is None:
                    raise IpcError("no-handler-for-request")
                result = await _invoke(registration[1], request)
                await send_frame(
                    writer,
                    {
                        "type": "response",
                        "requestId": request.get("requestId"),
                        "resultType": "success",
                        "method": method,
                        "handledByClientId": self._client_id,
                        "result": result,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - convert arbitrary handler errors at the protocol boundary
            try:
                if kind == "client-discovery-request":
                    await send_frame(
                        writer,
                        {
                            "type": "client-discovery-response",
                            "requestId": message.get("requestId"),
                            "response": {"canHandle": False},
                        },
                    )
                elif kind == "request":
                    await send_frame(
                        writer,
                        {
                            "type": "response",
                            "requestId": message.get("requestId"),
                            "resultType": "error",
                            "error": exc.code
                            if isinstance(exc, IpcError)
                            else "error-handling-request",
                        },
                    )
            except (OSError, ConnectionError):
                pass

    def _fail_pending(self, code):
        for future in self._pending.values():
            if not future.done():
                future.set_exception(IpcError(code))

    async def close(self):
        async with self._lifecycle_lock:
            await self._close()

    async def _close(self):
        if self._closed:
            return
        self._closed = True
        self._fail_pending("disposed")
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(IpcError("disposed"))
        if self._writer:
            self._writer.transport.abort()
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        if self._router:
            await self._router.close()
        if self._broadcast_task:
            self._broadcast_task.cancel()
            await asyncio.gather(self._broadcast_task, return_exceptions=True)
