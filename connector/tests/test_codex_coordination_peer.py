"""Owner/follower behavior over real Task 1 Unix transports."""

import asyncio
import tempfile
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

import pytest

from connector.runtimes.codex.coordination.peer import CoordinationPeer
from connector.runtimes.codex.coordination.transport import CoordinationClient
from connector.runtimes.codex.coordination.wire import IpcError


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 15))

    return run


@asynccontextmanager
async def peers(*options):
    with tempfile.TemporaryDirectory(prefix="aa-peer-", dir="/tmp") as directory:
        home = Path(directory)
        instances = [
            CoordinationPeer(
                CoordinationClient(home, endpoint=home / "ipc.sock"), **option
            )
            for option in (options or ({}, {}))
        ]
        try:
            for peer in instances:
                await peer.start()
            yield instances
        finally:
            for peer in reversed(instances):
                await peer.close()


def state(value=0):
    return {"id": "thread", "turns": [], "unknown": {"value": value}, "requests": []}


async def next_event(queue, method):
    while True:
        event = await asyncio.wait_for(queue.get(), 2)
        if event["method"] == method:
            return event


@async_test
async def test_follow_snapshot_repeated_follow_revision_and_isolated_copy():
    updates = asyncio.Queue()
    async with peers(
        {}, {"on_state": lambda thread, value: updates.put((thread, value))}
    ) as (owner, follower):
        assert await follower.follow("absent") is None
        await owner.claim("thread", state())
        info = await follower.discover_owner("thread")
        assert info.client_id == owner.client.client_id
        assert info.supports_untrusted_app_input is False
        assert await follower.follow("thread") == state()
        assert follower.is_follower("thread") and owner.is_owner("thread")
        assert (await updates.get())[1] == state()
        await follower.follow("thread")
        assert (await updates.get())[1] == state()  # A repeat really resends.
        revision = await owner.publish_state("thread", state(1))
        assert await follower.wait_revision("thread", revision) == state(1)
        copy = follower.get_state("thread")
        copy["unknown"]["value"] = 9
        assert follower.get_state("thread") == state(1)
        await follower.unfollow("thread")
        assert follower.get_state("thread") is None
        assert not follower.is_follower("thread")


@pytest.mark.parametrize(
    "method",
    [
        "thread-follower-start-turn",
        "thread-follower-load-complete-history",
        "thread-follower-compact-thread",
        "thread-follower-steer-turn",
        "thread-follower-interrupt-turn",
        "thread-follower-update-thread-settings",
        "thread-follower-update-daybreak",
        "thread-follower-edit-last-user-turn",
        "thread-follower-command-approval-decision",
        "thread-follower-file-approval-decision",
        "thread-follower-permissions-request-approval-response",
        "thread-follower-submit-user-input",
        "thread-follower-submit-mcp-server-elicitation-response",
        "thread-follower-set-queued-follow-ups-state",
    ],
)
@async_test
async def test_all_follower_routes_preserve_params_and_exact_result(method):
    received = []

    async def handle(name, params):
        received.append((name, params))
        return {"result": {"native": 17}, "future": True}

    async with peers({"owner_handler": handle}, {}) as (owner, follower):
        await owner.claim("thread", state())
        params = {"conversationId": "thread", "futureContext": {"opaque": [1, 2]}}
        result = await follower.request_owner("thread", method, params)
        assert result == {"result": {"native": 17}, "future": True}
        assert received == [(method, params)]


@async_test
async def test_host_isolation_and_owner_predicate_rechecked_on_execution():
    async with peers({}, {"host_id": "remote"}, {}) as (local, remote, caller):
        await remote.claim("thread", state(2))
        assert await caller.discover_owner("thread") is None
        await local.claim("thread", state(1))
        assert (await caller.follow("thread"))["unknown"]["value"] == 1
        assert (
            await remote.discover_owner("thread")
        ).client_id == remote.client.client_id
        await local.release("thread")
        with pytest.raises(IpcError, match="not-owner"):
            await local._handle_request(
                {
                    "method": "thread-follower-start-turn",
                    "params": {"conversationId": "thread"},
                }
            )


@async_test
async def test_response_items_gate_and_owner_pin_never_retargets():
    received = []

    async def handle(method, params):
        received.append(params)
        return {"ok": True}

    async with peers({"owner_handler": handle}, {}, {"owner_handler": handle}) as (
        owner,
        follower,
        next_owner,
    ):
        await owner.claim("thread", state())
        params = {
            "conversationId": "thread",
            "turnStart": {
                "request": {},
                "context": {"responseItems": [{"type": "opaque"}], "future": 42},
            },
        }
        with pytest.raises(IpcError, match="untrusted-app-input-unsupported"):
            await follower.request_owner("thread", "thread-follower-start-turn", params)
        assert received == []
        await owner.claim("thread", state(), supports_untrusted_app_input=True)
        await follower.request_owner("thread", "thread-follower-start-turn", params)
        assert received == [params]
        old_id = owner.client.client_id
        await owner.release("thread")
        await next_owner.claim("thread", state())
        with pytest.raises(IpcError):
            await follower.request_owner(
                "thread",
                "thread-follower-command-approval-decision",
                {"conversationId": "thread", "requestId": 3},
                expected_owner_client_id=old_id,
            )
        assert received == [params]


@async_test
async def test_patches_gap_and_invalid_batch_resync_target_current_owner():
    updates = asyncio.Queue()
    async with peers(
        {}, {"on_state": lambda thread, value: updates.put(value)}, {}
    ) as (owner, follower, attacker):
        await owner.claim("thread", state())
        await follower.follow("thread")
        await updates.get()
        revision = await owner.publish_patches(
            "thread", [{"op": "replace", "path": ["unknown", "value"], "value": 1}]
        )
        assert await follower.wait_revision("thread", revision) == state(1)
        await updates.get()
        # A foreign source cannot replace state, even with a plausible revision.
        await attacker.client.broadcast(
            "thread-stream-state-changed",
            {
                "hostId": "local",
                "conversationId": "thread",
                "change": {
                    "type": "snapshot",
                    "revision": 999,
                    "conversationState": state(99),
                },
            },
        )
        await owner.client.broadcast(
            "thread-stream-state-changed",
            {
                "hostId": "local",
                "conversationId": "thread",
                "change": {
                    "type": "patches",
                    "baseRevision": revision + 10,
                    "revision": revision + 11,
                    "patches": [],
                },
            },
        )
        assert await asyncio.wait_for(updates.get(), 2) == state(1)
        await owner.client.broadcast(
            "thread-stream-state-changed",
            {
                "hostId": "local",
                "conversationId": "thread",
                "change": {
                    "type": "patches",
                    "baseRevision": revision,
                    "revision": revision + 1,
                    "patches": [
                        {"op": "replace", "path": ["unknown", "value"], "value": 9},
                        {"op": "remove", "path": ["missing"]},
                    ],
                },
            },
        )
        assert await asyncio.wait_for(updates.get(), 2) == state(1)
        assert follower.get_revision("thread") == revision


@async_test
async def test_owner_disconnect_rejects_waiter_and_invalidates_state():
    events = asyncio.Queue()
    async with peers({}, {}, {"on_event": events.put}) as (_, owner, follower):
        await owner.claim("thread", state())
        await follower.follow("thread")
        pending = asyncio.create_task(follower.wait_revision("thread", 999))
        await owner.close()
        with pytest.raises(IpcError, match="owner-disconnected"):
            await pending
        assert follower.get_state("thread") is None
        assert follower.get_owner("thread") is None


@async_test
async def test_timeout_does_not_retry_or_become_owner_absence():
    calls = []

    async def handle(method, params):
        calls.append(params)
        await asyncio.Event().wait()

    async with peers({"owner_handler": handle}, {}) as (owner, follower):
        await owner.claim("thread", state())
        with pytest.raises(IpcError, match="timeout"):
            await follower.request_owner(
                "thread",
                "thread-follower-start-turn",
                {"conversationId": "thread"},
                timeout=0.05,
            )
        assert len(calls) == 1
        assert (
            await follower.discover_owner("thread")
        ).client_id == owner.client.client_id
        assert not follower.is_owner("thread")


@async_test
async def test_full_history_waits_for_returned_revision_and_partial_is_rejected():
    complete = {
        **state(),
        "turnHistory": {
            "kind": "canonical",
            "history": {
                "isComplete": True,
                "entitiesByKey": {},
                "islands": [{"entries": []}],
            },
        },
    }

    async def handle(method, params):
        assert method == "thread-follower-load-complete-history"
        revision = await owner.publish_state("thread", complete)
        return {"revision": revision}

    async with peers({"owner_handler": handle}, {}) as (owner, follower):
        await owner.claim("thread", state())
        assert await follower.load_complete_history("thread") == complete


@async_test
async def test_queue_metadata_events_keep_unknown_context_and_filter_host_account():
    events = asyncio.Queue()
    context = {
        "executionHostKey": "local:trusted",
        "identity": {"kind": "chatgpt", "accountId": "a", "userId": "u"},
        "unknown": [1],
    }
    async with peers(
        {}, {"on_event": events.put, "read_state_context": context}, {}
    ) as (owner, follower, stranger):
        await owner.claim("thread", state())
        await follower.follow("thread")
        params = {
            "conversationId": "thread",
            "messages": [{"id": "q", "future": True}],
            "pausedReason": "pending",
            "context": {"future": 1},
        }
        await owner.publish_event("thread-queued-followups-changed", params)
        event = await next_event(events, "thread-queued-followups-changed")
        assert event["params"] == {**params, "hostId": "local"}
        assert follower.get_queue("thread") == event["params"]
        await stranger.client.broadcast(
            "thread-queued-followups-changed",
            {**params, "hostId": "local", "messages": []},
        )
        await owner.publish_event(
            "thread-read-state-changed",
            {
                "conversationId": "thread",
                "context": {**context, "executionHostKey": "wrong"},
                "seen": 1,
            },
        )
        await owner.publish_event(
            "thread-read-state-changed",
            {"conversationId": "thread", "context": context, "seen": 2},
        )
        assert (await next_event(events, "thread-read-state-changed"))["params"][
            "seen"
        ] == 2
        assert follower.get_queue("thread")["messages"] == params["messages"]
        await owner.publish_event(
            "thread-archived", {"conversationId": "thread", "future": 4}
        )
        assert (await next_event(events, "thread-archived"))["params"]["future"] == 4


@async_test
async def test_follow_limit_is_explicit_and_unfollow_releases_capacity():
    async with peers({}, {"max_followed": 1}) as (owner, follower):
        await owner.claim("thread", state())
        await owner.claim("second", {"id": "second", "turns": []})
        await follower.follow("thread")
        with pytest.raises(IpcError, match="follow-limit"):
            await follower.follow("second")
        await follower.unfollow("thread")
        assert (await follower.follow("second"))["id"] == "second"


@async_test
async def test_reconnect_restores_owner_follows_and_revision_pin():
    updates = asyncio.Queue()
    async with peers({}, {"on_state": lambda thread, value: updates.put(value)}) as (
        owner,
        follower,
    ):
        await owner.claim("thread", state())
        await follower.follow("thread")
        await updates.get()
        previous = follower.client.client_id
        pending = asyncio.create_task(follower.wait_revision("thread", 999))
        follower.client._writer.transport.abort()  # Real connection loss, not a fake transport.
        with pytest.raises(IpcError, match="connection-reset"):
            await pending
        assert await asyncio.wait_for(updates.get(), 3) == state()
        assert follower.client.client_id != previous
        assert follower.get_owner("thread").client_id == owner.client.client_id
        previous = owner.client.client_id
        pending = asyncio.create_task(follower.wait_revision("thread", 999))
        owner.client._writer.transport.abort()
        with pytest.raises(IpcError, match="owner-disconnected"):
            await pending
        assert await asyncio.wait_for(updates.get(), 3) == state()
        assert owner.client.client_id != previous
        assert follower.get_owner("thread").client_id == owner.client.client_id
        assert owner.is_owner("thread")


@async_test
async def test_slow_callback_can_wait_for_later_revision_without_deadlock():
    entered, finished = asyncio.Event(), asyncio.Event()
    seen = []

    async def changed(thread, value):
        seen.append(value["unknown"]["value"])
        if len(seen) == 1:
            entered.set()
            await follower.wait_revision(thread, 3)
            finished.set()

    async with peers({}, {"on_state": changed}) as (owner, follower):
        await owner.claim("thread", state())
        await follower.follow("thread")
        await entered.wait()
        await owner.publish_state("thread", state(1))
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        assert seen == [0, 1]


@async_test
async def test_follow_timeout_differs_from_absence_and_disposal_rejects_waiter():
    async with peers({}, {}) as (native, follower):
        native.client.add_request_handler(
            "thread-owner-discovery",
            lambda _: True,
            lambda _: {"supportsUntrustedAppInput": False},
        )
        with pytest.raises(IpcError, match="follow-timeout"):
            await follower.follow("thread", timeout=0.03)
        assert not follower.is_owner("thread")
        with pytest.raises(IpcError, match="revision-timeout"):
            await follower.wait_revision("thread", 1, timeout=0.01)
        pending = asyncio.create_task(follower.wait_revision("thread", 1))
        await asyncio.sleep(0)
        await follower.close()
        with pytest.raises(IpcError, match="disposed"):
            await pending


@async_test
async def test_full_history_revision_timeout_and_incomplete_are_explicit():
    result = {"revision": 999}
    async with peers({"owner_handler": lambda method, params: result}, {}) as (
        owner,
        follower,
    ):
        await owner.claim("thread", state())
        with pytest.raises(IpcError, match="revision-timeout"):
            await follower.load_complete_history("thread", revision_timeout=0.01)
        result["revision"] = owner.get_revision("thread")
        with pytest.raises(IpcError, match="history-incomplete"):
            await follower.load_complete_history("thread")


@async_test
async def test_broadcast_version_mismatch_cannot_replace_snapshot():
    updates = asyncio.Queue()
    async with peers({}, {"on_state": lambda thread, value: updates.put(value)}) as (
        owner,
        follower,
    ):
        await owner.claim("thread", state())
        await follower.follow("thread")
        await updates.get()
        # Independent literal native envelope bypasses sender version generation.
        from connector.runtimes.codex.coordination.wire import send_frame

        await send_frame(
            owner.client._writer,
            {
                "type": "broadcast",
                "method": "thread-stream-state-changed",
                "sourceClientId": owner.client.client_id,
                "version": 10,
                "params": {
                    "hostId": "local",
                    "conversationId": "thread",
                    "change": {
                        "type": "snapshot",
                        "revision": 90,
                        "conversationState": state(90),
                    },
                },
            },
        )
        revision = await owner.publish_state("thread", state(1))
        assert await follower.wait_revision("thread", revision) == state(1)
        assert await updates.get() == state(1)


@async_test
async def test_read_state_ignored_without_local_context_and_queue_resends_on_follow():
    events = asyncio.Queue()
    async with peers({}, {"on_event": events.put}) as (owner, follower):
        await owner.claim("thread", state())
        await owner.publish_event(
            "thread-queued-followups-changed",
            {"conversationId": "thread", "messages": [{"id": "retained"}]},
        )
        await follower.follow("thread")
        assert (await next_event(events, "thread-queued-followups-changed"))["params"][
            "messages"
        ] == [{"id": "retained"}]
        await owner.publish_event(
            "thread-read-state-changed",
            {
                "conversationId": "thread",
                "context": {
                    "executionHostKey": "local:forged",
                    "identity": {"kind": "execution-storage", "authMode": "api-key"},
                },
            },
        )
        await owner.publish_event("thread-unarchived", {"conversationId": "thread"})
        while True:
            event = await asyncio.wait_for(events.get(), 1)
            assert event["method"] != "thread-read-state-changed"
            if event["method"] == "thread-unarchived":
                break


@async_test
async def test_foreign_lifecycle_broadcasts_cannot_invalidate_other_owner():
    updates = asyncio.Queue()
    async with peers(
        {}, {"on_state": lambda thread, value: updates.put(value)}, {}
    ) as (owner, follower, attacker):
        await owner.claim("thread", state())
        await follower.follow("thread")
        await updates.get()
        await attacker.client.broadcast(
            "client-status-changed",
            {"clientId": owner.client.client_id, "status": "disconnected"},
        )
        await attacker.client.broadcast("ipc-connection-reset", {})
        revision = await owner.publish_state("thread", state(1))
        assert await follower.wait_revision("thread", revision) == state(1)
        assert await updates.get() == state(1)


@async_test
async def test_remote_host_can_execute_but_local_request_cannot_reach_it():
    async with peers(
        {"host_id": "remote", "owner_handler": lambda method, params: {"ok": True}},
        {"host_id": "remote"},
        {},
    ) as (owner, remote, local):
        await owner.claim("thread", state())
        assert await remote.request_owner(
            "thread",
            "thread-follower-interrupt-turn",
            {"conversationId": "thread", "expectedTurnId": "turn"},
        ) == {"ok": True}
        with pytest.raises(IpcError, match="no-client-found"):
            await local.client.request(
                "thread-follower-interrupt-turn",
                {"conversationId": "thread", "expectedTurnId": "turn"},
                target_client_id=owner.client.client_id,
            )


@async_test
async def test_owner_revision_waiter_resolves_when_first_follow_increments_revision():
    async with peers({}, {}) as (owner, follower):
        initial = await owner.claim("thread", state())
        waiting = asyncio.create_task(
            owner.wait_revision("thread", initial + 1, timeout=0.1)
        )
        await follower.follow("thread")
        assert await waiting == state()


@async_test
async def test_close_finishes_with_backpressured_unsubscribe_and_rejects_follow():
    # Reuse only the fresh transport suite's real stalled native socket fixture.
    from test_codex_coordination_transport import stalled_native

    with tempfile.TemporaryDirectory(prefix="aa-close-", dir="/tmp") as directory:
        async with stalled_native(Path(directory)) as client:
            peer = CoordinationPeer(client)
            await peer.start()
            following = asyncio.create_task(peer.follow("thread"))
            await asyncio.sleep(0.01)
            flooding = asyncio.create_task(
                client.broadcast("large", {"data": "x" * 4_000_000})
            )
            try:
                await asyncio.sleep(0.03)
                assert client._writer.transport.get_write_buffer_size() > 0
                await asyncio.wait_for(peer.close(), 0.8)
                with pytest.raises(IpcError, match="disposed"):
                    await following
                await asyncio.wait_for(
                    asyncio.gather(flooding, return_exceptions=True), 0.1
                )
            finally:
                following.cancel()
                flooding.cancel()
                await asyncio.gather(following, flooding, return_exceptions=True)
                await peer.close()


@async_test
async def test_discovered_owner_without_operation_handler_is_not_native_fallback():
    async with peers({}, {}) as (owner, follower):
        await owner.claim("thread", state())
        with pytest.raises(IpcError, match="unsupported-operation"):
            await follower.request_owner(
                "thread", "thread-follower-start-turn", {"conversationId": "thread"}
            )
        assert (
            await follower.discover_owner("thread")
        ).client_id == owner.client.client_id
        assert not follower.is_owner("thread")


@async_test
async def test_new_owner_status_request_rebinds_after_release_without_disconnect():
    updates = asyncio.Queue()
    async with peers(
        {}, {"on_state": lambda thread, value: updates.put(value)}, {}
    ) as (owner, follower, next_owner):
        await owner.claim("thread", state())
        await follower.follow("thread")
        await updates.get()
        # Settle the initial connected-event restore before exercising migration.
        await asyncio.sleep(0.03)
        while not updates.empty():
            updates.get_nowait()
        old_id = owner.client.client_id
        waiting = asyncio.create_task(
            follower.wait_revision("thread", 999, expected_owner_client_id=old_id)
        )
        await owner.release("thread")
        await next_owner.claim("thread", state(2))
        with pytest.raises(IpcError, match="owner-changed"):
            await asyncio.wait_for(waiting, 0.3)
        assert await asyncio.wait_for(updates.get(), 0.3) == state(2)
        assert follower.get_owner("thread").client_id == next_owner.client.client_id
