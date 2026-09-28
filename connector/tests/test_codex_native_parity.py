"""Native activity ordering and App-compatible steering at production boundaries."""

import asyncio
from copy import deepcopy

import pytest
from connector.runtime_protocol import RuntimeAttachment, RuntimeAttachmentContent
from connector.runtimes.codex.coordination.projection import (
    active_turn,
    state_to_native,
)
from connector.runtimes.codex.coordination.reducer import reduce_event
from connector.runtimes.codex.domain.thread_state import thread_status
from connector.runtimes.codex.sdk.events import CodexSdkEvent
from connector.runtimes.codex.timeline.accumulator import CodexTimelineAccumulator
from test_codex_coordination_operations import async_test
from test_codex_runtime_coordination_review import owner_state, real_runtime


@pytest.fixture(autouse=True)
def attachment_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments")
    )


def event(method, **params):
    return {"method": method, "params": {"threadId": "remote", **params}}


def started(turn_id):
    return event(
        "turn/started", turn={"id": turn_id, "status": "inProgress", "items": []}
    )


def idle():
    return event("thread/status/changed", status={"type": "idle"})


def active_state():
    return reduce_event(
        {
            **owner_state(),
            "cwd": "/actual/native",
            "threadRuntimeStatus": {"type": "idle"},
        },
        started("original"),
    )


def test_ordered_idle_disables_residual_turn_without_rewriting_native_history():
    state = reduce_event(active_state(), started("residual"))
    state = reduce_event(
        state, event("turn/completed", turn={"id": "original", "status": "completed"})
    )
    state = reduce_event(state, idle())
    assert active_turn(state) is None
    assert thread_status(state_to_native(state)) == "idle"
    assert state["turns"][-1]["status"] == "inProgress"
    assert state["threadGoal"] == {"objective": "keep goal"}
    state = reduce_event(state, started("new"))
    assert active_turn(state)["turnId"] == "new"
    assert thread_status(state_to_native(state)) == "running"
    # Late completion of another turn must not erase the newer active target.
    state = reduce_event(
        state, event("turn/completed", turn={"id": "original", "status": "completed"})
    )
    assert active_turn(state)["turnId"] == "new"


@async_test
async def test_real_projection_idle_and_start_ack_ordering(tmp_path):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        native.responses["thread/resume"] = {
            "thread": {"id": "remote", "status": {"type": "idle"}, "turns": []}
        }

        async def respond(_):
            await native.handler(started("ack"), 1)
            await native.handler(idle(), 1)
            await native.handler(started("newer"), 1)
            return {"turn": {"id": "ack", "status": "inProgress", "items": []}}

        native.responses["turn/start"] = respond
        result = await runtime.start_turn("view", "remote", "first")
        assert result.result["turnId"] == "ack"
        assert runtime._active_turn_ids["view"] == "newer"
        await native.handler(idle(), 1)
        await adapter.refresh_state("remote", force=True)
        assert "view" not in runtime._active_turn_ids
        assert runtime._session_states.get("view").status == "idle"
        assert active_turn(adapter.peer.get_state("remote")) is None


@pytest.mark.parametrize("newer", ["idle", "completed", "active", None])
@async_test
async def test_real_start_after_idle_does_not_repaint_newer_native_observation(
    tmp_path, newer
):
    async with real_runtime(tmp_path) as (runtime, _host, _adapter, _owner, native):
        native.responses["thread/resume"] = {
            "thread": {"id": "remote", "status": {"type": "idle"}, "turns": []}
        }

        async def respond(_):
            if newer == "idle":
                await native.handler(idle(), 1)
            elif newer == "completed":
                await native.handler(
                    event("turn/completed", turn={"id": "ack", "status": "completed"}),
                    1,
                )
            elif newer == "active":
                await native.handler(started("newer"), 1)
            return {"turn": {"id": "ack", "status": "inProgress", "items": []}}

        native.responses["turn/start"] = respond
        await runtime.start_turn("view", "remote", "first")
        target = runtime._active_turn_ids.get("view")
        assert target == (
            "newer" if newer == "active" else "ack" if newer is None else None
        )
        assert runtime._session_states.get("view").status == (
            "running" if target else "idle"
        )


@pytest.mark.parametrize("kind", ["enteredReviewMode", "exitedReviewMode"])
def test_review_markers_hidden_from_raw_and_typed_visible_timeline(kind):
    from openai_codex.generated.v2_all import ItemCompletedNotification

    raw = {"id": "marker", "type": kind, "review": "No findings"}
    timeline = CodexTimelineAccumulator()
    params = {"threadId": "remote", "turnId": "review", "item": raw, "completedAtMs": 1}
    assert (
        timeline.item_from_notification("view", "remote", "item/completed", params)
        is None
    )
    typed = ItemCompletedNotification.model_validate(params)
    sdk_event = CodexSdkEvent.from_parts(
        event_type="item/completed",
        params=params,
        raw=event("item/completed", **params),
        payload=typed,
    )
    assert timeline.item_from_event("view", "remote", sdk_event) is None
    items = timeline.items_from_thread_snapshot(
        "view",
        "remote",
        {
            "turns": [
                {
                    "id": "review",
                    "items": [
                        raw,
                        {"id": "final", "type": "agentMessage", "text": "No findings"},
                        {"id": "other", "type": "futureMarker"},
                    ],
                }
            ]
        },
        None,
    )
    assert len(items) == 2
    assert any(item.content.get("kind") == "unknown" for item in items)


@pytest.mark.parametrize("later", ["idle", "newer"])
@pytest.mark.parametrize(
    "attachment_kinds", [(), ("image",), ("file",), ("image", "file")]
)
@async_test
async def test_real_app_consumer_restore_attachments_ack_target_and_order(
    tmp_path, later, attachment_kinds
):
    async with real_runtime(tmp_path) as (runtime, host, adapter, owner, native):
        await owner.claim("remote", active_state())
        await runtime.prepare_session_view("view", "remote")
        for fid, mime in (("image", "image/png"), ("file", "text/plain")):
            host.attachments[fid] = RuntimeAttachmentContent(
                file_id=fid, name=fid, media_type=mime, content=b"test"
            )
        calls = []

        async def consume(method, params):
            # Installed App QBt reads text_elements.length before optimistic
            # insertion or native dispatch. SDK schema defaults are not applied.
            for item in params["input"]:
                if item["type"] == "text":
                    if not isinstance(item.get("text_elements"), list):
                        raise ValueError(
                            "native text parser requires text_elements array"
                        )
                    assert len(item["text_elements"]) == 0
            calls.append(deepcopy(params))
            restore = params["restoreMessage"]
            assert restore["cwd"] == "/actual/native"
            assert restore["context"]["workspaceRoots"] == ["/actual/native"]
            assert "collaborationMode" not in restore["context"]
            assert restore["text"] == restore["context"]["prompt"] == "  exact draft\n"
            assert restore["id"] == params["clientUserMessageId"]
            pending = runtime._pending_messages.pending_message_by_client_id(
                "remote", restore["id"]
            )
            assert pending.text == restore["text"]
            assert isinstance(restore["createdAt"], int)
            assert len(params["attachments"]) == len(attachment_kinds)
            assert len(params["input"]) == 1 + ("image" in attachment_kinds)
            assert params["input"][0]["text"].count("[Attached file:") == (
                "file" in attachment_kinds
            )
            if "image" in attachment_kinds:
                assert params["input"][1]["type"] == "localImage"
                assert (
                    restore["context"]["imageAttachments"][0]["localPath"]
                    == params["input"][1]["path"]
                )
            else:
                assert restore["context"]["imageAttachments"] == []
            current = reduce_event(owner.get_state("remote"), idle())
            if later == "newer":
                current = reduce_event(current, started("newer"))
            await owner.publish_state("remote", current)
            await adapter.peer._notifications.join()
            return {"result": {"turnId": "actual-native-target"}}

        owner.owner_handler = consume
        result = await runtime.steer_turn(
            "view",
            "remote",
            "  exact draft\n",
            attachments=tuple(
                RuntimeAttachment(file_id=fid, name=fid, media_type=mime)
                for fid, mime in (("image", "image/png"), ("file", "text/plain"))
                if fid in attachment_kinds
            ),
        )
        assert result.ok
        assert result.result["turnId"] == "actual-native-target"
        assert runtime._active_turn_ids.get("view") == (
            "newer" if later == "newer" else None
        )
        assert runtime._session_states.get("view").status == (
            "running" if later == "newer" else "idle"
        )
        assert len(calls) == 1
        assert native.calls == []


@pytest.mark.parametrize(
    "ack", [{}, {"result": {}}, {"result": {"turnId": None}}, {"result": {"turnId": 4}}]
)
@async_test
async def test_malformed_steer_ack_is_not_success_or_retried(tmp_path, ack):
    async with real_runtime(tmp_path) as (runtime, _host, _adapter, owner, native):
        await owner.claim("remote", active_state())
        await runtime.prepare_session_view("view", "remote")
        calls = []

        async def consume(method, params):
            calls.append(params)
            return ack

        owner.owner_handler = consume
        with pytest.raises((ValueError, RuntimeError), match="acknowledg"):
            await runtime.steer_turn(
                "view", "remote", "exact", client_message_id="logical"
            )
        assert len(calls) == 1
        assert native.calls == []


@async_test
async def test_idle_residual_cannot_be_steered_or_stopped_but_queue_can_advance(
    tmp_path,
):
    from connector.runtimes.codex.coordination.queue import execute_head
    from test_codex_coordination_operations import setup

    operations, native, peer, journal = setup(
        tmp_path, [{"id": "residual", "status": "inProgress", "items": []}]
    )
    peer.state = reduce_event(peer.state, idle())
    with pytest.raises(ValueError, match="no active"):
        await operations.handle(
            "thread-follower-steer-turn",
            {"conversationId": "t", "input": [{"type": "text", "text": "no"}]},
        )
    stopped = await operations.handle(
        "thread-follower-interrupt-turn",
        {"conversationId": "t", "mode": "system", "expectedTurnId": "residual"},
    )
    assert stopped["interruptedTurnId"] is None
    assert native.calls == []
    await operations.handle(
        "thread-follower-set-queued-follow-ups-state",
        {"conversationId": "t", "state": {"t": [{"id": "queued", "text": "next"}]}},
    )
    native.responses["turn/start"] = {
        "turn": {"id": "next", "status": "inProgress", "items": []}
    }
    assert await execute_head(operations, "t") is True
    assert active_turn(peer.state)["turnId"] == "next"
    assert journal.queue("t") == []


@pytest.mark.parametrize("ack", [{"turnId": "accepted"}, {}, {"turnId": None}])
@async_test
async def test_real_aa_owner_steer_retains_typed_attachments_and_journal(tmp_path, ack):
    from connector.runtimes.codex.sdk.runtime_client import (
        CodexSteerTurnRequest,
        CodexTurnInputAttachment,
    )

    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        native.responses["thread/resume"] = {
            "thread": {"id": "remote", "cwd": "/native", "turns": []}
        }
        native.responses["turn/start"] = {
            "turn": {"id": "active", "status": "inProgress", "items": []}
        }
        await runtime.start_turn("view", "remote", "start")
        request = CodexSteerTurnRequest(
            thread_id="remote",
            turn_id="active",
            content="exact",
            client_message_id="logical",
            attachments=(
                CodexTurnInputAttachment("image", "/tmp/image.png", "image/png"),
                CodexTurnInputAttachment("note", "/tmp/note.txt", "text/plain"),
            ),
        )
        native.responses["turn/steer"] = ack
        if ack.get("turnId"):
            result = await adapter.steer_turn(request)
            assert result.turn_id == "accepted"
        else:
            with pytest.raises(ValueError, match="acknowledgement"):
                await adapter.steer_turn(request)
        sent = [params for method, params in native.calls if method == "turn/steer"]
        assert len(sent) == 1
        assert sent[0]["expectedTurnId"] == "active"
        assert sent[0]["input"][1] == {"type": "localImage", "path": "/tmp/image.png"}
        assert sent[0]["input"][0]["text"].count("/tmp/note.txt") == 1
        record = adapter.journal.operation("remote")
        assert record["request"]["restoreMessage"]["text"] == "exact"
        assert record["request"]["restoreMessage"]["id"] == "logical"
        assert record["stage"] == ("confirmed" if ack.get("turnId") else "unknown")
        if not ack.get("turnId"):
            with pytest.raises(ValueError, match="unconfirmed"):
                await adapter.steer_turn(request)
            assert len([m for m, _ in native.calls if m == "turn/steer"]) == 1


@async_test
async def test_steer_never_acquires_owner_or_silently_drops_attachment(tmp_path):
    from connector.runtimes.codex.sdk.runtime_client import CodexSteerTurnRequest

    async with real_runtime(tmp_path) as (runtime, _host, adapter, owner, native):
        with pytest.raises(ValueError, match="existing observed owner"):
            await adapter.steer_turn(CodexSteerTurnRequest("remote", "active", "exact"))
        assert native.calls == []
        await owner.claim("remote", active_state())
        await runtime.prepare_session_view("view", "remote")
        calls = []

        async def consume(method, params):
            calls.append(params)
            return {"result": {"turnId": "active"}}

        owner.owner_handler = consume
        with pytest.raises(Exception, match="materialization failed"):
            await runtime.steer_turn(
                "view",
                "remote",
                "exact",
                attachments=(RuntimeAttachment(file_id="missing"),),
            )
        assert calls == []
        assert native.calls == []


@async_test
async def test_sdk_steer_uses_real_sdk_input_conversion_and_actual_ack(monkeypatch):
    from connector.runtimes.codex.sdk.client import CodexSdkClient
    from connector.runtimes.codex.sdk.runtime_client import (
        CodexSteerTurnRequest,
        CodexTurnInputAttachment,
    )
    from openai_codex._inputs import _to_wire_input
    from openai_codex.generated.v2_all import TurnSteerResponse

    sent = []

    class Turn:
        id = "expected"

        async def steer(self, input):
            sent.extend(_to_wire_input(input))
            return TurnSteerResponse(turnId="actual")

    client = object.__new__(CodexSdkClient)
    monkeypatch.setattr(client, "_turn_handle", lambda **_: Turn())
    result = await client.steer_turn(
        CodexSteerTurnRequest(
            "remote",
            "expected",
            "exact",
            attachments=(
                CodexTurnInputAttachment("image", "/tmp/image", "image/png"),
                CodexTurnInputAttachment("file", "/tmp/file", "text/plain"),
            ),
        )
    )
    assert sent[1] == {"type": "localImage", "path": "/tmp/image"}
    assert sent[0]["text"].count("/tmp/file") == 1
    assert result.turn_id == "actual"


@pytest.mark.parametrize("operation", ["start", "steer"])
@pytest.mark.parametrize(
    "during",
    ["unchanged", "settings-goal-history", "idle-patch", "active-idle", "newer"],
)
@async_test
async def test_follower_ack_activity_epoch_ignores_unrelated_revisions(
    tmp_path, operation, during
):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, owner, _native):
        initial = (
            active_state()
            if operation == "steer"
            else {**owner_state(), "threadRuntimeStatus": {"type": "idle"}}
        )
        initial.pop("aaActivity", None)
        if operation == "steer":
            initial["threadRuntimeStatus"] = {"type": "active"}
        await owner.claim("remote", initial)
        await runtime.prepare_session_view("view", "remote")

        async def consume(method, params):
            current = owner.get_state("remote")
            if during == "settings-goal-history":
                current.update(
                    latestModel="changed",
                    threadGoal={"objective": "new goal", "status": "active"},
                )
                current["turns"] = [
                    {"turnId": "old-history", "status": "completed", "items": []},
                    *current["turns"],
                ]
                await owner.publish_state("remote", current)
            elif during == "idle-patch":
                await owner.publish_patches(
                    "remote",
                    [
                        {
                            "op": "replace",
                            "path": ["threadRuntimeStatus"],
                            "value": {"type": "idle"},
                        }
                    ],
                )
            elif during in {"active-idle", "newer"}:
                # App-style canonical records without AA activity extensions.
                current.pop("aaActivity", None)
                current["threadRuntimeStatus"] = {"type": "active"}
                current["turns"].append(
                    {"turnId": "newer", "status": "inProgress", "items": []}
                )
                await owner.publish_state("remote", current)
                if during == "active-idle":
                    current["threadRuntimeStatus"] = {"type": "idle"}
                    await owner.publish_state("remote", current)
            if operation == "start":
                return {
                    "result": {
                        "turn": {"id": "ack", "status": "inProgress", "items": []}
                    }
                }
            return {"result": {"turnId": "ack"}}

        owner.owner_handler = consume
        if operation == "start":
            result = await runtime.start_turn("view", "remote", "exact")
        else:
            result = await runtime.steer_turn("view", "remote", "exact")
        assert result.ok and result.result["turnId"] == "ack"
        expected = (
            "ack"
            if during in {"unchanged", "settings-goal-history"}
            else "newer"
            if during == "newer"
            else None
        )
        assert runtime._active_turn_ids.get("view") == expected
        # Passive reads and projection use the same activity evidence.
        observed = await adapter.read_thread("remote")
        assert thread_status(observed.thread) == ("running" if expected else "idle")
        if during == "settings-goal-history":
            assert observed.thread["threadGoal"]["objective"] == "new goal"
        # A later explicit idle supersedes a positive ACK, without altering turns.
        await owner.publish_patches(
            "remote",
            [
                {
                    "op": "replace",
                    "path": ["threadRuntimeStatus"],
                    "value": {"type": "idle"},
                }
            ],
        )
        await adapter.peer.wait_revision(
            "remote", owner.get_revision("remote"), timeout=2
        )
        await adapter.peer._notifications.join()
        await adapter.refresh_state("remote", force=True)
        assert runtime._active_turn_ids.get("view") is None
        assert (
            adapter.peer.get_state("remote")["turns"]
            == owner.get_state("remote")["turns"]
        )


@async_test
async def test_cold_complete_snapshot_removes_persisted_review_markers_without_erasing_partial_history(
    tmp_path,
):
    async with real_runtime(tmp_path) as (runtime, host, adapter, owner, _native):
        state = owner_state()
        state["turnsPagination"]["hasLoadedOldest"] = False
        state["turns"] = [
            {
                "turnId": "review",
                "status": "completed",
                "items": [
                    {"id": "entered", "type": "enteredReviewMode", "review": "Review"},
                    {
                        "id": "exited",
                        "type": "exitedReviewMode",
                        "review": "No findings",
                    },
                    {"id": "final", "type": "agentMessage", "text": "No findings"},
                ],
            }
        ]
        persisted = {
            "entered": "old unknown row",
            "exited": "old unknown row",
            "older": "unloaded history",
        }
        original_sync = host.timeline_sync

        async def sync(**params):
            if params["complete"]:
                persisted.clear()
            persisted.update({item.id: item for item in params["items"]})
            await original_sync(**params)

        host.timeline_sync = sync
        await owner.claim("remote", state)
        await runtime.prepare_session_view("view", "remote")
        assert "older" in persisted and "entered" in persisted
        # The normal full-history path supplies an authoritative complete snapshot.
        state["turnsPagination"]["hasLoadedOldest"] = True
        state["turns"].insert(
            0,
            {
                "turnId": "history",
                "status": "completed",
                "items": [
                    {"id": "older", "type": "agentMessage", "text": "Old history"}
                ],
            },
        )
        runtime._notifications.coordination.published.clear()
        runtime._notifications.coordination.published_complete.clear()
        revision = await owner.publish_state("remote", state)
        await adapter.peer.wait_revision("remote", revision, timeout=2)
        await adapter.refresh_state("remote", force=True)
        assert set(persisted) == {"older", "final"}
        assert persisted["final"].content["text"] == "No findings"
        assert len(adapter.peer.get_state("remote")["turns"][-1]["items"]) == 3
        assert host.timeline_syncs[-1]["complete"] is True


@pytest.mark.parametrize("kind", ["enteredReviewMode", "exitedReviewMode"])
def test_typed_sdk_snapshot_suppresses_review_markers(kind):
    from openai_codex.generated.v2_all import Thread, ThreadItem, Turn, TurnStatus

    thread = Thread.model_construct(
        id="remote",
        turns=[
            Turn.model_construct(
                id="review",
                status=TurnStatus.completed,
                items=[
                    ThreadItem.model_validate(
                        {"id": "marker", "type": kind, "review": "No findings"}
                    ),
                    ThreadItem.model_validate(
                        {"id": "final", "type": "agentMessage", "text": "No findings"}
                    ),
                ],
            )
        ],
    )
    items = CodexTimelineAccumulator().items_from_sdk_thread_snapshot(
        "view", "remote", thread, None
    )
    assert len(items) == 1 and items[0].content["text"] == "No findings"


@pytest.mark.parametrize("operation", ["start", "steer"])
@async_test
async def test_native_idle_during_ack_publication_is_not_repainted(tmp_path, operation):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        native.responses["thread/resume"] = {
            "thread": {"id": "remote", "status": {"type": "idle"}, "turns": []}
        }
        native.responses["turn/start"] = {
            "turn": {"id": "ack", "status": "inProgress", "items": []}
        }
        native.responses["turn/steer"] = {"turnId": "ack"}
        if operation == "steer":
            await runtime.start_turn("view", "remote", "first")
        ready, release = asyncio.Event(), asyncio.Event()
        publish = adapter.peer._snapshot

        async def delayed(thread_id, targets=None):
            if (
                active_turn(adapter.peer.get_state(thread_id)) is not None
                and not ready.is_set()
            ):
                ready.set()
                await release.wait()
            return await publish(thread_id, targets)

        adapter.peer._snapshot = delayed
        task = asyncio.create_task(
            runtime.start_turn("view", "remote", "first")
            if operation == "start"
            else runtime.steer_turn("view", "remote", "follow")
        )
        await asyncio.wait_for(ready.wait(), 2)
        await native.handler(idle(), 1)
        release.set()
        assert (await task).ok
        assert runtime._active_turn_ids.get("view") is None
        assert runtime._session_states.get("view").status == "idle"
        assert adapter.peer.get_state("remote")["turns"][0]["status"] == "inProgress"


@async_test
async def test_confirmed_start_after_idle_during_journal_preparation_is_newer(tmp_path):
    async with real_runtime(tmp_path) as (runtime, _host, adapter, _owner, native):
        native.responses["thread/resume"] = {
            "thread": {"id": "remote", "status": {"type": "idle"}, "turns": []}
        }
        native.responses["turn/start"] = {
            "turn": {"id": "ack", "status": "inProgress", "items": []}
        }
        stage = adapter.journal.stage

        async def journal_stage(thread_id, name, **fields):
            await stage(thread_id, name, **fields)
            if name == "starting":
                await native.handler(idle(), 1)

        adapter.journal.stage = journal_stage
        await runtime.start_turn("view", "remote", "first")
        assert runtime._active_turn_ids.get("view") == "ack"


@async_test
async def test_attachment_descriptor_mismatch_fails_before_native_dispatch(tmp_path):
    from test_codex_coordination_operations import setup

    operations, native, _peer, journal = setup(
        tmp_path, [{"id": "active", "status": "inProgress", "items": []}]
    )
    with pytest.raises(ValueError, match="missing from native input"):
        await operations.handle(
            "thread-follower-steer-turn",
            {
                "conversationId": "t",
                "input": [{"type": "text", "text": "exact"}],
                "attachments": [
                    {
                        "label": "image",
                        "path": "/tmp/img",
                        "fsPath": "/tmp/img",
                        "isImageAttachment": True,
                    }
                ],
            },
        )
    assert native.calls == []
    assert journal.operation("t") is None
