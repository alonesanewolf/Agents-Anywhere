"""Command endpoint dispatch boundaries; isolated SQLite fixture only."""

import pytest
from test_backend_mvp import FakeLocalRpc, create_connector_and_session, make_client

from agent_server.infra.connector_rpc import ConnectorOfflineError


def setup(tmp_path, outcome=None):
    client = make_client(tmp_path)
    _, _, session_id, headers = create_connector_and_session(client)

    class Rpc(FakeLocalRpc):
        calls = 0

        async def request(self, connector_id, method, params, *, timeout=30):
            if method == "session.command.execute":
                self.calls += 1
                if isinstance(outcome, Exception):
                    raise outcome
                if outcome is not None:
                    return outcome
            return await super().request(connector_id, method, params, timeout=timeout)

    rpc = Rpc()
    client.app.state.rpc = rpc
    client.post(f"/sessions/{session_id}/takeover", headers=headers).raise_for_status()
    return client, f"/sessions/{session_id}/runtime/commands", headers, rpc


@pytest.mark.parametrize("raw", ["", "/goal create first\n second  "])
def test_exact_raw_including_empty(tmp_path, raw):
    client, url, headers, rpc = setup(tmp_path)
    response = client.post(url, headers=headers, json={"command": "goal", "raw": raw})
    assert response.status_code == 200
    assert rpc.requests[-1][2]["raw"] == raw


def test_catalog_query_limit(tmp_path):
    client, url, headers, rpc = setup(tmp_path)
    response = client.get(
        url, headers=headers, params={"query": " Goal ", "limit": 999}
    )
    assert response.status_code == 200
    assert rpc.requests[-1][2]["query"] == " Goal "
    assert rpc.requests[-1][2]["limit"] == 999


@pytest.mark.parametrize("limit", [0, -1, 1001, "1.5", "true"])
def test_catalog_invalid_limit(tmp_path, limit):
    client, url, headers, _ = setup(tmp_path)
    assert client.get(url, headers=headers, params={"limit": limit}).status_code == 422


@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError("secret"),
        ConnectorOfflineError("secret"),
        {},
        {"ok": "true"},
        {"command": "other", "ok": True, "result": {}},
    ],
)
def test_unknown_dispatch_is_not_retried_or_success(tmp_path, outcome):
    client, url, headers, rpc = setup(tmp_path, outcome)
    response = client.post(url, headers=headers, json={"command": "compact"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert data["code"] == "command_outcome_unknown"
    assert data["result"] == {"executionState": "unknown", "retryable": False}
    assert "secret" not in response.text
    assert rpc.calls == 1


def test_known_native_error_is_preserved(tmp_path):
    result = {
        "command": "compact",
        "ok": False,
        "code": "command_error",
        "message": "busy",
        "result": {"executionState": "completed"},
    }
    client, url, headers, rpc = setup(tmp_path, result)
    data = client.post(url, headers=headers, json={"command": "compact"}).json()
    assert {key: data[key] for key in result} == result
    assert rpc.calls == 1
