"""Runtime presentation-only notifications traverse ingest, cache and WebSocket."""

from __future__ import annotations

import asyncio

import pytest
from test_backend_mvp import (
    create_connector_and_session,
    make_client,
    receive_session_ws_event,
    ws_ticket,
)


def test_goal_and_control_metadata_publishes_without_timeline_advance(tmp_path):
    with make_client(tmp_path) as client:
        _, token, session_id, headers = create_connector_and_session(client)
        ticket = ws_ticket(client, session_id, headers)
        connector_headers = {"Authorization": f"Bearer {token}"}
        goal = {
            "threadId": "thread-native",
            "objective": "Ship IPC",
            "status": "active",
            "tokensUsed": 5,
            "timeUsedSeconds": 8,
            "tokenBudget": None,
        }
        metadata = {
            "codexPresentation": {"threadGoal": goal, "completedThreadGoal": None}
        }

        def send_metadata(value):
            response = client.post(
                "/connector/ingest",
                headers=connector_headers,
                json={
                    "notifications": [
                        {
                            "method": "session.state.updated",
                            "params": {
                                "sessionId": session_id,
                                "runtime": "codex",
                                "status": "idle",
                                "selections": {},
                                "metadata": value,
                            },
                        }
                    ]
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["rejected"] == []

        with client.websocket_connect(
            f"/sessions/{session_id}/ws?ticket={ticket}"
        ) as ws:
            assert ws.receive_json()["type"] == "session.subscribed"
            baseline_sequence = None

            def expect_state(expected_metadata):
                nonlocal baseline_sequence
                send_metadata(expected_metadata)
                event = receive_session_ws_event(ws, "runtime.state.updated", timeout=1)
                state = event["payload"]["state"]
                assert state["metadata"] == expected_metadata
                assert state["status"] == "idle"
                assert state["selections"] == {}
                if baseline_sequence is None:
                    baseline_sequence = event["sequence"]
                else:
                    assert event["sequence"] == baseline_sequence
                cached = client.get(
                    f"/sessions/{session_id}/runtime/state", headers=headers
                )
                assert cached.status_code == 200, cached.text
                assert cached.json()["state"]["metadata"] == expected_metadata
                assert cached.json()["state"]["updatedSeq"] == baseline_sequence

            expect_state(metadata)  # paused/idle goal creation
            goal = {**goal, "objective": "Ship IPC with smoke tests"}
            metadata = {
                "codexPresentation": {"threadGoal": goal, "completedThreadGoal": None}
            }
            expect_state(metadata)  # edit
            goal = {**goal, "tokenBudget": 7000}
            metadata = {
                "codexPresentation": {"threadGoal": goal, "completedThreadGoal": None}
            }
            expect_state(metadata)  # budget
            goal = {**goal, "tokensUsed": 12, "timeUsedSeconds": 15}
            metadata = {
                "codexPresentation": {"threadGoal": goal, "completedThreadGoal": None}
            }
            expect_state(metadata)  # displayed usage
            goal = {**goal, "status": "paused"}
            metadata = {
                "codexPresentation": {"threadGoal": goal, "completedThreadGoal": None}
            }
            expect_state(metadata)  # pause
            metadata = {
                "codexPresentation": {"threadGoal": None, "completedThreadGoal": None}
            }
            expect_state(metadata)  # explicit clear
            goal = {**goal, "status": "complete"}
            metadata = {
                "codexPresentation": {"threadGoal": None, "completedThreadGoal": goal}
            }
            expect_state(metadata)  # completed goal remains visible
            # Change each consumed field separately so no other field can
            # accidentally make a missing projection appear to work.
            for update in (
                {"codexCoordination": {"role": "owner"}},
                {"codexCoordination": {"role": "owner", "available": True}},
                {
                    "codexCoordination": {
                        "role": "owner",
                        "available": True,
                        "generation": 4,
                    }
                },
                {"codexCapabilities": {"goalControl": True}},
                {"codexCapabilities": {"goalControl": True, "userSessionStop": True}},
                {
                    "codexSettings": {
                        "latestThreadSettings": {"collaborationMode": {"mode": "plan"}}
                    }
                },
            ):
                metadata = {**metadata, **update}
                expect_state(metadata)
            metadata = {
                **metadata,
                "codexCoordination": {
                    "role": "follower",
                    "available": False,
                    "generation": 5,
                },
            }
            expect_state(metadata)
            metadata = {
                **metadata,
                "codexSettings": {
                    "latestThreadSettings": {"collaborationMode": {"mode": "default"}},
                },
            }
            expect_state(metadata)

            noisy = {
                **metadata,
                "source": "stream",
                "transportRevision": 99,
                "codexPresentation": {
                    **metadata["codexPresentation"],
                    "revision": 17,
                    "completedThreadGoal": {**goal, "debug": {"revision": 55}},
                },
                "codexCoordination": {**metadata["codexCoordination"], "revision": 18},
                "codexCapabilities": {**metadata["codexCapabilities"], "revision": 19},
                "codexSettings": {
                    **metadata["codexSettings"],
                    "latestTokenUsageInfo": {"total": 100},
                },
            }
            send_metadata(noisy)
            cached = asyncio.run(
                client.app.state.session_runtime_state_cache.get(session_id)
            )
            assert cached is not None
            assert cached.metadata == metadata
            assert cached.updatedSeq == baseline_sequence
            with pytest.raises(
                AssertionError, match="did not receive runtime.state.updated"
            ):
                receive_session_ws_event(ws, "runtime.state.updated", timeout=0.2)


def test_omitted_goal_fact_and_explicit_null_are_distinct():
    """A missing goal field is not interpreted as a clear notification."""
    from agent_server.core.models import SessionRuntimeState
    from agent_server.services.connector_ingest import runtime_states_semantically_equal

    base = SessionRuntimeState(
        sessionId="sess_goal",
        runtime="codex",
        status="idle",
        updatedSeq=1,
        createdAt="2026-09-28T00:00:00Z",
        updatedAt="2026-09-28T00:00:00Z",
        metadata={"codexPresentation": {}},
    )
    cleared = base.model_copy(
        update={
            "metadata": {"codexPresentation": {"threadGoal": None}},
            "updatedSeq": 2,
        }
    )
    assert not runtime_states_semantically_equal(base, cleared)
    assert runtime_states_semantically_equal(
        cleared,
        cleared.model_copy(
            update={
                "metadata": {
                    "codexPresentation": {"threadGoal": None, "revision": 999},
                    "codexCoordination": {"revision": 3},
                },
                "updatedSeq": 3,
            }
        ),
    )
