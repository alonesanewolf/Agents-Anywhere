import tempfile
from pathlib import Path

import pytest
from test_codex_coordination_operations import Native, async_test

from connector.core.json_kv import JsonKeyValueStore
from connector.runtimes.codex.coordination.peer import CoordinationPeer
from connector.runtimes.codex.coordination.transport import CoordinationClient
from connector.runtimes.codex.sdk.runtime_client import CodexStartTurnRequest


class RuntimeNative(Native):
    def set_native_event_handler(self, handler):
        self.handler = handler

    async def start(self, handler):
        self.normalized = handler

    async def stop(self):
        pass

    async def native_thread_resume(self, thread_id):
        return await self.native_request("thread/resume", {"threadId": thread_id})


@async_test
async def test_passive_cold_read_never_claims_and_remote_send_never_mutates_local(
    tmp_path,
):
    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient

    with tempfile.TemporaryDirectory(prefix="aa-coord-", dir="/tmp") as directory:
        endpoint = Path(directory) / "ipc.sock"
        owner = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        follower = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        native = RuntimeNative()
        native.responses["thread/read"] = {"thread": {"id": "cold", "turns": []}}
        events, forwarded = [], []

        async def event(value):
            events.append(value)

        async def operation(method, params):
            forwarded.append((method, params))
            return {"result": {"turn": {"id": "remote-turn", "status": "inProgress"}}}

        adapter = CoordinatedCodexClient(
            native,
            follower,
            kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
            namespace="runtime",
        )
        await owner.start()
        await adapter.start(event)
        try:
            result = await adapter.read_thread("cold")
            assert result.thread["id"] == "cold"
            assert not follower.is_owner("cold")
            assert [call[0] for call in native.calls] == ["thread/read"]
            owner.owner_handler = operation
            await owner.claim(
                "remote",
                {
                    "id": "remote",
                    "resumeState": "resumed",
                    "turns": [],
                    "turnsPagination": {"hasLoadedOldest": True},
                    "requests": [],
                    "future": {"x": 1},
                },
            )
            await adapter.attach_thread("remote")
            read = await adapter.read_thread("remote")
            assert read.thread["future"] == {"x": 1}
            started = await adapter.start_turn(
                CodexStartTurnRequest(thread_id="remote", content="hi")
            )
            assert started.turn_id == "remote-turn"
            assert (
                forwarded[0][1]["turnStart"]["context"]["inheritThreadSettings"] is True
            )
            await native.handler(
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": "remote", "goal": {"status": "wrong"}},
                },
                1,
            )
            assert follower.get_state("remote").get("threadGoal") is None
            assert [call[0] for call in native.calls] == ["thread/read"]
        finally:
            await adapter.stop()
            await owner.close()


@async_test
async def test_aa_owner_raw_state_and_response_context_reject_reused_id(tmp_path):
    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient

    with tempfile.TemporaryDirectory(prefix="aa-coord-", dir="/tmp") as directory:
        endpoint = Path(directory) / "ipc.sock"
        peer = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        native = RuntimeNative()
        native.responses["thread/resume"] = {"thread": {"id": "t", "turns": []}}
        native.responses["turn/start"] = {
            "turn": {"id": "physical", "status": "inProgress", "items": []}
        }
        events = []

        async def event(value):
            events.append(value)

        adapter = CoordinatedCodexClient(
            native,
            peer,
            kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
            namespace="runtime",
        )
        await adapter.start(event)
        try:
            result = await adapter.start_turn(
                CodexStartTurnRequest(thread_id="t", content="start")
            )
            assert result.turn_id == "physical"
            request = {
                "id": 7,
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "t", "questions": []},
            }
            await native.handler(request, 1)
            await adapter.refresh_state("t")
            shown = events[-1]["params"]
            token = shown["requestContexts"][0]["responseContext"]
            await adapter.refresh_state("t", force=True)
            assert (
                events[-1]["params"]["requestContexts"][0]["responseContext"] == token
            )
            await adapter.respond_to_request(token, {"answers": {}})
            assert native.calls[-1][0] == "respond"
            await native.handler(request, 1)
            await adapter.refresh_state("t", force=True)
            fresh = events[-1]["params"]["requestContexts"][0]["responseContext"]
            assert fresh != token
            with pytest.raises(ValueError, match="stale"):
                await adapter.respond_to_request(token, {"answers": {}})
            native.native_generation = 2
            with pytest.raises(ValueError, match="stale"):
                await adapter.respond_to_request(fresh, {"answers": {}})
        finally:
            await adapter.stop()
