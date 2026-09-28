"""Passive runtime publication and snapshot cursor consistency at route boundaries."""

import asyncio

import pytest
from starlette.requests import HTTPConnection
from test_backend_mvp import (
    FakeLocalRpc,
    create_connector_and_session,
    make_client,
    receive_session_ws_event,
    ws_ticket,
)

from agent_server.api import sessions as session_routes
from agent_server.core.models import SessionRuntimeState
from agent_server.deps import get_connector_ingest_service


class PassiveRpc(FakeLocalRpc):
    def __init__(self, original):
        super().__init__()
        self._original = original

    async def close(self):
        await self._original.close()


def _cached_state(client, session_id, metadata):
    persisted = asyncio.run(
        client.app.state.store.get_session_runtime_state(session_id)
    )
    state = persisted.model_copy(update={"metadata": metadata})
    asyncio.run(client.app.state.session_runtime_state_cache.put(state))
    return state


def _connector_state(state: SessionRuntimeState, metadata):
    return {
        "sessionId": state.sessionId,
        "runtime": state.runtime,
        "runtimeId": state.runtimeId,
        "externalSessionId": state.externalSessionId,
        "status": state.status,
        "selections": state.selections,
        "metadata": metadata,
    }


def test_background_refresh_publishes_ownerless_recovery_without_sequence_change(
    tmp_path,
):
    with make_client(tmp_path) as client:
        _, _, session_id, headers = create_connector_and_session(client)
        waiting = _cached_state(
            client,
            session_id,
            {
                "source": "codex.view.preparing",
                "codexCoordination": {"role": "unattached", "available": False},
            },
        )
        recovered = {
            "source": "codex.thread/read.state",
            "codexCoordination": {"role": "unattached", "available": True},
        }
        rpc = PassiveRpc(client.app.state.rpc)
        rpc.runtime_states[session_id] = _connector_state(waiting, recovered)
        client.app.state.rpc = rpc
        ticket = ws_ticket(client, session_id, headers)

        with client.websocket_connect(
            f"/sessions/{session_id}/ws?ticket={ticket}"
        ) as ws:
            subscribed = ws.receive_json()
            snapshot = client.get(f"/sessions/{session_id}/snapshot", headers=headers)
            assert snapshot.status_code == 200, snapshot.text
            assert (
                snapshot.json()["state"]["metadata"]["codexCoordination"]["available"]
                is False
            )
            event = receive_session_ws_event(ws, "runtime.state.updated", timeout=1)
            assert event["payload"]["state"]["status"] == "idle"
            assert event["payload"]["state"]["metadata"]["codexCoordination"] == {
                "role": "unattached",
                "available": True,
            }
            assert event["sequence"] == subscribed["sequence"]


def test_background_refresh_publishes_goal_and_settings_but_ignores_diagnostics(
    tmp_path,
):
    with make_client(tmp_path) as client:
        _, _, session_id, headers = create_connector_and_session(client)
        before = _cached_state(
            client,
            session_id,
            {
                "codexPresentation": {
                    "completedThreadGoal": {
                        "objective": "Earlier goal",
                        "status": "complete",
                    }
                },
                "codexSettings": {
                    "latestThreadSettings": {
                        "collaborationMode": {"mode": "default"},
                    }
                },
            },
        )
        goal_changed = {
            "codexPresentation": {
                "completedThreadGoal": {
                    "objective": "Current goal",
                    "status": "complete",
                }
            },
            "codexSettings": {
                "latestThreadSettings": {
                    "collaborationMode": {"mode": "default"},
                }
            },
        }
        changed = {
            **goal_changed,
            "codexSettings": {
                "latestThreadSettings": {
                    "collaborationMode": {"mode": "plan"},
                }
            },
        }
        rpc = PassiveRpc(client.app.state.rpc)
        rpc.runtime_states[session_id] = _connector_state(before, goal_changed)
        client.app.state.rpc = rpc
        ticket = ws_ticket(client, session_id, headers)

        with client.websocket_connect(
            f"/sessions/{session_id}/ws?ticket={ticket}"
        ) as ws:
            subscribed = ws.receive_json()
            response = client.get(f"/sessions/{session_id}/snapshot", headers=headers)
            assert response.status_code == 200, response.text
            event = receive_session_ws_event(ws, "runtime.state.updated", timeout=1)
            assert event["payload"]["state"]["metadata"] == goal_changed
            assert event["sequence"] == subscribed["sequence"]

            rpc.runtime_states[session_id] = _connector_state(before, changed)
            response = client.get(f"/sessions/{session_id}/snapshot", headers=headers)
            assert response.status_code == 200, response.text
            event = receive_session_ws_event(ws, "runtime.state.updated", timeout=1)
            assert event["payload"]["state"]["metadata"] == changed
            assert event["sequence"] == subscribed["sequence"]

            noisy = {
                **changed,
                "source": "codex.thread/read.state",
                "transportRevision": 999,
                "codexPresentation": {
                    **changed["codexPresentation"],
                    "revision": 33,
                },
            }
            rpc.runtime_states[session_id] = _connector_state(before, noisy)
            response = client.get(f"/sessions/{session_id}/snapshot", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["eventCursor"] == subscribed["cursor"]
            with pytest.raises(
                AssertionError, match="did not receive runtime.state.updated"
            ):
                receive_session_ws_event(ws, "runtime.state.updated", timeout=0.2)


def test_snapshot_reconciles_late_completed_event_before_returning_cursor(
    tmp_path, monkeypatch
):
    with make_client(tmp_path) as client:
        connector_id, _, session_id, headers = create_connector_and_session(client)
        persisted = asyncio.run(
            client.app.state.store.set_session_status(session_id, "waiting_approval")
        )
        old = _cached_state(
            client,
            session_id,
            {
                "codexCoordination": {"role": "unattached", "available": False},
            },
        )
        assert (
            old.status == "waiting_approval" and old.updatedSeq == persisted.updatedSeq
        )
        finished = {
            "codexCoordination": {"role": "unattached", "available": True},
            "codexPresentation": {
                "completedThreadGoal": {
                    "objective": "Finished",
                    "status": "complete",
                }
            },
        }
        rpc = PassiveRpc(client.app.state.rpc)
        rpc.runtime_states[session_id] = {
            **_connector_state(old, finished),
            "status": "idle",
        }
        client.app.state.rpc = rpc
        original = session_routes.read_session_capabilities_with_fallback
        injected = False

        async def late_ingest(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                connection = HTTPConnection({"type": "http", "app": client.app})
                ingest = get_connector_ingest_service(connection)
                await ingest.handle_notification_message(
                    connector_id=connector_id,
                    method="session.state.updated",
                    params={
                        **_connector_state(old, finished),
                        "status": "idle",
                    },
                )
            return await original(*args, **kwargs)

        monkeypatch.setattr(
            session_routes, "read_session_capabilities_with_fallback", late_ingest
        )
        ticket = ws_ticket(client, session_id, headers)
        with client.websocket_connect(
            f"/sessions/{session_id}/ws?ticket={ticket}"
        ) as ws:
            ws.receive_json()
            response = client.get(f"/sessions/{session_id}/snapshot", headers=headers)
            assert response.status_code == 200, response.text
            snapshot = response.json()
            event = receive_session_ws_event(ws, "runtime.state.updated", timeout=1)
            assert event["payload"]["state"]["status"] == "idle"
            assert snapshot["state"]["status"] == "idle"
            assert snapshot["state"]["metadata"] == finished
            assert snapshot["session"]["status"] == "idle"
            assert snapshot["eventCursor"] == event["cursor"]
            assert snapshot["timeline"]["nextSeq"] == event["sequence"]
