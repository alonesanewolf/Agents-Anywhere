"""Ordered raw events and exact server requests from the SDK's sole reader."""

import asyncio
from copy import deepcopy


def request_key(value):
    if type(value) not in (str, int):
        raise ValueError("invalid native request id")
    return type(value), value


class NativeEventBridge:
    def __init__(self, handler, *, generation):
        self.handler = handler
        self.generation = generation
        self.pending = {}
        self.queue = asyncio.Queue()
        self.closed = False
        self.task = None

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self._run())

    def raw_tap(self, message):
        self.loop.call_soon_threadsafe(self._receive, deepcopy(message))

    def _receive(self, message):
        if self.closed:
            return
        if "id" in message:
            key = request_key(message["id"])
            if key in self.pending:
                self.terminate(ValueError("duplicate native request id"))
                return
            future = self.loop.create_future()
            # Error completion can precede the worker beginning its wait.
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            self.pending[key] = (message, future)
        if message.get("method") == "serverRequest/resolved":
            params = message.get("params", {})
            entry = self.pending.get(request_key(params.get("requestId")))
            if entry and not entry[1].done():
                entry[1].set_exception(ValueError("native request already resolved"))
        self.queue.put_nowait(message)

    async def _run(self):
        while True:
            message = await self.queue.get()
            try:
                await self.handler(message, self.generation)
            except Exception as exc:  # noqa: BLE001 - transport consumer failure boundary
                self.terminate(exc)
                return
            finally:
                self.queue.task_done()

    def server_request(self, message):
        return asyncio.run_coroutine_threadsafe(
            self.wait_response(message), self.loop
        ).result()

    async def wait_response(self, message):
        key = request_key(message["id"])
        entry = self.pending.get(key)
        if entry is None:
            raise ValueError("stale native request")
        try:
            return await asyncio.shield(entry[1])
        finally:
            if self.pending.get(key) is entry:
                self.pending.pop(key, None)

    async def respond(self, request_id, result, *, generation, thread_id, method):
        entry = self.pending.get(request_key(request_id))
        if (
            self.closed
            or generation != self.generation
            or entry is None
            or entry[1].done()
            or entry[0]["method"] != method
            or entry[0].get("params", {}).get("threadId") != thread_id
        ):
            raise ValueError("stale native request")
        entry[1].set_result(deepcopy(dict(result)))

    def terminate(self, error):
        if self.closed:
            return
        self.closed = True
        for _, future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        # The consumer learns authority has ended even without another event.
        self.loop.create_task(
            self.handler(
                {"method": "native/disconnected", "params": {}}, self.generation
            )
        )

    def reader_closed(self, error):
        self.loop.call_soon_threadsafe(self.terminate, error)

    async def close(self):
        self.terminate(RuntimeError("native connection closed"))
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.pending.clear()
