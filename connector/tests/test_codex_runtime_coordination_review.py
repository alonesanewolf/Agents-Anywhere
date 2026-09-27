"""Task4 review regressions at the production facade/peer boundary."""

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from test_codex_coordination_adapter import RuntimeNative
from test_codex_runtime import FakeHost
from test_codex_runtime_coordination import turn

from connector.core.json_kv import JsonKeyValueStore
from connector.runtime_protocol import RuntimeConfig
from connector.runtimes.codex.coordination.client import CoordinatedCodexClient
from connector.runtimes.codex.coordination.peer import CoordinationPeer
from connector.runtimes.codex.coordination.transport import CoordinationClient
from connector.runtimes.codex.runtime import CodexRuntime
from connector.runtimes.codex.sdk.runtime_client import CodexModelListResult


@asynccontextmanager
async def real_runtime(tmp_path):
    with tempfile.TemporaryDirectory(prefix="aa-t4-review-", dir="/tmp") as directory:
        endpoint = Path(directory) / "ipc.sock"
        owner = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        follower = CoordinationPeer(CoordinationClient(directory, endpoint=endpoint))
        native = RuntimeNative()

        async def models():
            return CodexModelListResult(models=())

        native.list_models = models
        host = FakeHost()
        adapter = CoordinatedCodexClient(
            native,
            follower,
            kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
            namespace="review",
        )
        runtime = CodexRuntime(
            RuntimeConfig(runtime="codex", revision=8), host, adapter
        )
        await owner.start()
        await runtime.start()
        try:
            yield runtime, host, adapter, owner, native
        finally:
            await runtime.stop()
            await owner.close()


def owner_state(*, request=False):
    return {
        "id": "remote",
        "turns": [],
        "requests": [
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "remote", "command": "ls"},
            }
        ]
        if request
        else [],
        "threadGoal": {"objective": "keep goal"},
        "turnsPagination": {"hasLoadedOldest": True},
        "resumeState": "resumed",
    }


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


def test_real_passive_read_cleanup_does_not_publish_false_unavailability(tmp_path):
    async def run():
        async with real_runtime(tmp_path) as (runtime, host, adapter, owner, native):
            await owner.claim("remote", owner_state())
            result = await runtime.get_session_state("inventory-session", "remote")
            assert result.status == "idle"
            await adapter.peer._notifications.join()
            # Read the actual current facade payload after its scoped unfollow.
            assert adapter._snapshot("remote")[3] == "none"
            await adapter.refresh_state("remote", force=True)
            cached = runtime._session_states.get_by_external_session_id("remote")
            assert cached is None or cached.status != "blocked"
            assert not any(
                update["metadata"].get("codexCoordination", {}).get("available")
                is False
                for update in host.state_updates
            )
            assert not adapter.peer.is_follower("remote")
            assert not adapter.peer.is_owner("remote")
            assert native.calls == []

    asyncio.run(run())


def test_real_explicit_follower_disconnect_closes_notice_and_reconnects(tmp_path):
    async def run():
        async with real_runtime(tmp_path) as (runtime, host, adapter, owner, native):
            await owner.claim("remote", owner_state(request=True))
            await runtime.prepare_session_view("view", "remote")
            previous = (await runtime.get_session_notices("view"))[0]
            previous_client = adapter.peer.client.client_id
            # Real socket loss; production transport restores the same followed thread.
            adapter.peer.client._writer.transport.abort()
            await until(
                lambda: (
                    adapter.peer.client.client_id != previous_client
                    and adapter.peer.get_state("remote") is not None
                    and any(u["status"] == "blocked" for u in host.state_updates)
                )
            )
            await adapter.peer._notifications.join()
            await adapter.refresh_state("remote", force=True)
            state = await runtime.get_session_state("view")
            assert state.metadata["codexCoordination"]["available"] is True
            assert state.metadata["codexPresentation"]["threadGoal"] == {
                "objective": "keep goal"
            }
            assert runtime._notices.get(previous.notice_id).status == "closed"
            current = (await runtime.get_session_notices("view"))[0]
            assert current.notice_id != previous.notice_id
            with pytest.raises(ValueError):
                await runtime.respond_interaction("view", previous.notice_id, "approve")
            assert native.calls == []

    asyncio.run(run())


def test_real_native_owner_loss_with_role_none_remains_unavailable(tmp_path):
    async def run():
        async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
            native.responses["thread/resume"] = {"thread": owner_state(request=True)}
            native.responses["turn/start"] = {
                "turn": {"id": "active", "status": "inProgress", "items": []}
            }
            await runtime.start_turn("owned", "remote", "hello")
            await adapter.refresh_state("remote", force=True)
            notice = (await runtime.get_session_notices("owned"))[0]
            assert adapter.peer.is_owner("remote")
            await native.handler({"method": "native/disconnected", "params": {}}, 1)
            assert adapter._snapshot("remote")[3] == "none"
            state = await runtime.get_session_state("owned")
            assert state.status == "blocked"
            assert state.metadata["codexCoordination"]["available"] is False
            assert runtime._notices.get(notice.notice_id).status == "closed"
            assert "owned" not in runtime._active_turn_ids
            with pytest.raises(ValueError):
                await runtime.start_turn("owned", "remote", "must not start")

    asyncio.run(run())


@pytest.mark.parametrize("items", [[], [turn("survivor", "same content")]])
def test_identical_partial_to_complete_clears_unknown_persisted_items(tmp_path, items):
    async def run():
        async with real_runtime(tmp_path) as (runtime, host, adapter, owner, _native):
            # Server data from a previous runtime is unknown to this projector.
            persisted = {"obsolete-from-prior-runtime"}
            original_sync = host.timeline_sync

            async def sync(**kwargs):
                if kwargs["complete"]:
                    persisted.clear()
                persisted.update(item.id for item in kwargs["items"])
                await original_sync(**kwargs)

            host.timeline_sync = sync
            state = owner_state()
            state["turns"] = [{**item, "turnId": item["id"]} for item in items]
            state["turnsPagination"]["hasLoadedOldest"] = False
            await owner.claim("remote", state)
            await runtime.prepare_session_view("view", "remote")
            await adapter.peer._notifications.join()
            assert "obsolete-from-prior-runtime" in persisted
            count = len(host.timeline_syncs)
            state["turnsPagination"]["hasLoadedOldest"] = True
            revision = await owner.publish_state("remote", state)
            await adapter.peer.wait_revision("remote", revision)
            await adapter.peer._notifications.join()
            await adapter.refresh_state("remote", force=True)
            assert len(host.timeline_syncs) == count + 1
            assert host.timeline_syncs[-1]["complete"] is True
            assert "obsolete-from-prior-runtime" not in persisted
            count = len(host.timeline_syncs)
            # Same contents and complete authority at another real peer revision.
            revision = await owner.publish_state("remote", state)
            await adapter.peer.wait_revision("remote", revision)
            await adapter.peer._notifications.join()
            await adapter.refresh_state("remote", force=True)
            assert len(host.timeline_syncs) == count

    asyncio.run(run())


def test_real_scoped_detach_closes_previously_displayed_response_context(tmp_path):
    async def run():
        async with real_runtime(tmp_path) as (runtime, host, adapter, owner, native):
            await owner.claim("remote", owner_state(request=True))
            await runtime.prepare_session_view("view", "remote")
            notice = (await runtime.get_session_notices("view"))[0]
            before_detach = len(host.state_updates)
            await adapter.detach_thread("remote")
            assert adapter._snapshot("remote")[3] == "none"
            await adapter.refresh_state("remote", force=True)
            assert runtime._notices.get(notice.notice_id).status == "closed"
            assert (await runtime.get_session_notices("view")) == ()
            assert not any(
                update["status"] == "blocked"
                for update in host.state_updates[before_detach:]
            )
            with pytest.raises(ValueError):
                await runtime.respond_interaction("view", notice.notice_id, "approve")
            assert native.calls == []

    asyncio.run(run())
