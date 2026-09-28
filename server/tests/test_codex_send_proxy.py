"""Actual FastAPI route + runtime dispatcher + installed Next proxy, private sockets."""

import asyncio
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from agent_server.api import sessions
from agent_server.services.session_run import SessionRunService
from agent_server.services.session_runtime_state_cache import SessionRuntimeStateCache
from fastapi import FastAPI
from test_codex_passive_view_preflight import (
    SCALE,
    Devices,
    RpcBoundary,
    Store,
    deadlines,  # noqa: F401 - imported autouse fixture
    runtime_network,
)


class RouteStore(Store):
    async def set_session_status(self, _, status):
        self.session = self.session.model_copy(update={"status": status})
        return self.session

    async def get_session_seq(self, _):
        return 1

    @asynccontextmanager
    async def session_revision_fence(self, _):
        yield


class Broker:
    def __init__(self):
        self.published = asyncio.Event()

    async def publish(self, *_):
        self.published.set()


@pytest.mark.parametrize(
    "outer_seconds,accepted", [(30, False), (100, True), (20, False)]
)
def test_proxy_deadline_preserves_one_backend_send(outer_seconds, accepted):
    async def run():
        async with runtime_network() as c:
            rpc, store, broker = RpcBoundary(c), RouteStore(), Broker()
            app = FastAPI()
            app.include_router(sessions.router, prefix="/api/v2")
            service = SessionRunService(store, rpc, Devices())
            cache = SessionRuntimeStateCache()
            app.dependency_overrides.update(
                {
                    sessions.current_user_id: lambda: "user",
                    sessions.get_session_run_service: lambda: service,
                    sessions.get_store: lambda: store,
                    sessions.get_timeline_broker: lambda: broker,
                    sessions.get_rpc: lambda: rpc,
                    sessions.get_session_runtime_state_cache: lambda: cache,
                }
            )
            # Extra independent capability latency makes the 30s envelope fail
            # deterministically; actual router absence still consumes 3 x10s.
            request = rpc.request

            async def delayed(connector, method, params, timeout=30):
                if method == "session.capabilities":
                    await asyncio.sleep(0.06)
                return await request(connector, method, params, timeout)

            rpc.request = delayed
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(app, log_level="error", lifespan="off")
            )
            task = asyncio.create_task(server.serve(sockets=[listener]))
            proxy = None
            try:
                while not server.started:
                    await asyncio.sleep(0.005)
                script = (
                    Path(__file__).resolve().parents[2]
                    / "web-next/test/next-proxy-fixture.mjs"
                )
                proxy = await asyncio.create_subprocess_exec(
                    "node",
                    str(script),
                    f"http://127.0.0.1:{port}",
                    {30: "legacy", 100: "configured", 20: "short"}[outer_seconds],
                    str(SCALE),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                proxy_port = int(await asyncio.wait_for(proxy.stdout.readline(), 3))
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"http://127.0.0.1:{proxy_port}/api/v2/sessions/session/runtime/messages",
                        json={"content": "once", "clientMessageId": "proxy-once"},
                    )
                await asyncio.wait_for(broker.published.wait(), 4)
                assert response.status_code == (200 if accepted else 500)
                if accepted:
                    assert response.json()["ok"]
                assert len([p for m, p in c.native.calls if m == "turn/start"]) == 1
                assert len([p for m, p in c.native.calls if m == "thread/resume"]) == 1
                assert rpc.calls == [
                    ("session.state", 20),
                    ("session.capabilities", 10),
                    ("session.send_message", 30),
                    ("session.state", 20),
                    ("session.capabilities", 10),
                ]
            finally:
                await rpc.drain()
                if proxy:
                    proxy.terminate()
                    await asyncio.wait_for(proxy.communicate(), 3)
                server.should_exit = True
                await asyncio.wait_for(task, 3)
                listener.close()

    asyncio.run(run())
