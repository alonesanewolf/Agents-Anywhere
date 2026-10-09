"""Tests for the headless CLI runtimes (MiniMax Code / CodeBuddy)."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from connector.core.json_kv import JsonKeyValueStore
from connector.runtime_protocol import RuntimeConfig
from connector.runtime_protocol.host import RuntimeHostClient
from connector.runtimes import default_runtime_providers
from connector.runtimes.cli_headless import HeadlessCliRuntime, HeadlessCliSpec
from connector.runtimes.cli_headless.provider_config import codebuddy_argv
from connector.server.capabilities import protocol_capabilities_from_runtime_types

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

CODEBUDDY_STREAM = [
    '{"type":"system","subtype":"init","session_id":"cb-123"}',
    '{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"pondering"}}}',
    '{"type":"stream_event","event":{"type":"content_block_stop","index":0}}',
    '{"type":"stream_event","event":{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"hello"}}}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":" world"}}}',
    '{"type":"stream_event","event":{"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"t1","name":"Read"}}}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\\"file_path\\": \\"a.txt\\"}"}}}',
    '{"type":"stream_event","event":{"type":"content_block_stop","index":2}}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"hello world done"}]}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"hello world done"}',
]

MCODE_STREAM = [
    '{"schemaVersion":1,"sequence":1,"type":"session.started","sessionId":"mvs_abc"}',
    '{"schemaVersion":1,"sequence":2,"type":"turn.started"}',
    '{"schemaVersion":1,"sequence":3,"type":"item.started","item":{"id":"i1","type":"agent_message","contentDelta":"this is "}}',
    '{"schemaVersion":1,"sequence":4,"type":"item.updated","item":{"id":"i1","type":"agent_message","contentDelta":"a delta"}}',
    '{"schemaVersion":1,"sequence":5,"type":"item.completed","item":{"id":"i1","type":"agent_message","content":"this is the full answer."}}',
    '{"schemaVersion":1,"sequence":6,"type":"turn.completed","usage":{}}',
]


class FakeHost(RuntimeHostClient):
    """Minimal host that records everything a runtime publishes."""

    def __init__(self, kv: JsonKeyValueStore) -> None:
        self._kv = kv
        self.items: list[Any] = []
        self.metas: list[dict[str, Any]] = []
        self.states: list[str | None] = []
        self.turn_outcomes: list[str] = []

    @property
    def runtime_kv(self) -> JsonKeyValueStore:
        return self._kv

    @property
    def connector_id(self) -> str:
        return "test-connector"

    async def timeline_item_upsert(self, item: Any) -> None:
        self.items.append(item)

    async def session_meta_upsert(self, **kwargs: Any) -> None:
        self.metas.append(kwargs)

    async def session_state_update(self, **kwargs: Any) -> None:
        self.states.append(kwargs.get("status"))

    async def session_turn_ended(self, **kwargs: Any) -> None:
        self.turn_outcomes.append(kwargs.get("outcome"))


def make_spec(lines: list[str]) -> HeadlessCliSpec:
    """A spec whose 'CLI' prints the given NDJSON lines and exits 0."""

    inner = f"import sys\nfor line in {lines!r}:\n    sys.stdout.write(line + chr(10))\n"

    def build_argv(
        prompt: str,
        workspace: str | None,
        model: str | None,
        cli_session: str | None,
        attachments: tuple = (),
    ) -> list[str] | None:
        return [sys.executable, "-c", inner]

    return HeadlessCliSpec(
        key="fakecli",
        display_name="Fake",
        description="test kernel",
        available=lambda: True,
        build_argv=build_argv,
        models=(("m-fast", "Fast"), ("m-full", "Full")),
    )


async def run_turn(
    tmp_path: str, spec: HeadlessCliSpec, config_values: dict[str, Any] | None = None
) -> tuple[FakeHost, HeadlessCliRuntime, Any]:
    kv_path = os.path.join(tmp_path, "kv.json")
    host = FakeHost(JsonKeyValueStore(kv_path))
    runtime = HeadlessCliRuntime(
        config=RuntimeConfig(runtime="fakecli", revision=1, values=dict(config_values or {})),
        host=host,
        spec=spec,
    )
    await runtime.start()
    result = await runtime.create_and_start_session(
        session_id="s1", content="go", client_message_id="cm-1"
    )
    assert result.ok
    state = None
    for _ in range(200):
        await asyncio.sleep(0.05)
        state = await runtime.get_session_state("s1")
        if state is not None and state.status in ("idle", "error"):
            break
    assert state is not None and state.status == "idle", host.turn_outcomes
    snapshot = await runtime.get_session_snapshot("s1")
    await runtime.stop()
    return host, runtime, snapshot


# --------------------------------------------------------------------------
# Stream parsing
# --------------------------------------------------------------------------


def test_codebuddy_stream_json_turn(tmp_path: Any) -> None:
    host, _runtime, snapshot = asyncio.run(
        run_turn(str(tmp_path), make_spec(CODEBUDDY_STREAM))
    )
    assert host.turn_outcomes == ["completed"]
    assistants = [i for i in snapshot.items if i.type == "message" and i.role == "assistant"]
    # The final assistant event carries the authoritative full text.
    assert assistants[-1].content["text"] == "hello world done"
    assert assistants[-1].status == "done"
    reasoning = [
        i
        for i in snapshot.items
        if i.type == "system" and i.content.get("kind") == "reasoning"
    ]
    assert reasoning and "pondering" in reasoning[-1].content["text"]
    tools = [i for i in snapshot.items if i.type == "tool"]
    assert len(tools) == 1
    assert tools[0].content["title"] == "Read"
    assert tools[0].content["input"] == {"file_path": "a.txt"}


def test_mcode_stream_json_turn(tmp_path: Any) -> None:
    host, runtime, snapshot = asyncio.run(run_turn(str(tmp_path), make_spec(MCODE_STREAM)))
    assert host.turn_outcomes == ["completed"]
    # The native session id is captured for multi-turn resume.
    assert runtime._sessions["s1"].cli_session_id == "mvs_abc"
    assistants = [i for i in snapshot.items if i.type == "message" and i.role == "assistant"]
    assert assistants[-1].content["text"] == "this is the full answer."


def test_legacy_plain_text_fallback(tmp_path: Any) -> None:
    host, _runtime, snapshot = asyncio.run(
        run_turn(str(tmp_path), make_spec(["plain line 1", "plain line 2"]))
    )
    assert host.turn_outcomes == ["completed"]
    assistants = [i for i in snapshot.items if i.type == "message" and i.role == "assistant"]
    assert "plain line 1" in assistants[-1].content["text"]


def test_nonzero_exit_fails_turn(tmp_path: Any) -> None:
    inner = (
        "import sys\n"
        "sys.stdout.write('{\"type\":\"result\",\"is_error\":true,\"result\":\"boom\"}' + chr(10))\n"
        "sys.exit(1)\n"
    )
    spec = HeadlessCliSpec(
        key="fakecli",
        display_name="Fake",
        description="t",
        available=lambda: True,
        build_argv=lambda prompt, workspace, model, cli_session, attachments: [
            sys.executable,
            "-c",
            inner,
        ],
    )
    kv_path = os.path.join(str(tmp_path), "kv.json")
    host = FakeHost(JsonKeyValueStore(kv_path))
    runtime = HeadlessCliRuntime(
        config=RuntimeConfig(runtime="fakecli", revision=1, values={}), host=host, spec=spec
    )

    async def scenario() -> None:
        await runtime.start()
        await runtime.create_and_start_session("s1", "go", client_message_id="cm-1")
        for _ in range(200):
            await asyncio.sleep(0.05)
            state = await runtime.get_session_state("s1")
            if state is not None and state.status in ("idle", "error"):
                break
        assert state is not None and state.status == "error"
        await runtime.stop()

    asyncio.run(scenario())
    assert host.turn_outcomes == ["failed"]


# --------------------------------------------------------------------------
# Model selection
# --------------------------------------------------------------------------


def test_model_selection_prefers_session_selection(tmp_path: Any) -> None:
    seen: list[str | None] = []

    def build_argv(
        prompt: str,
        workspace: str | None,
        model: str | None,
        cli_session: str | None,
        attachments: tuple = (),
    ) -> list[str] | None:
        seen.append(model)
        return [sys.executable, "-c", "pass"]

    spec = HeadlessCliSpec(
        key="fakecli",
        display_name="Fake",
        description="t",
        available=lambda: True,
        build_argv=build_argv,
        models=(("m-fast", "Fast"), ("m-full", "Full")),
    )
    kv_path = os.path.join(str(tmp_path), "kv.json")
    host = FakeHost(JsonKeyValueStore(kv_path))
    runtime = HeadlessCliRuntime(
        config=RuntimeConfig(runtime="fakecli", revision=1, values={"defaultModel": "m-fast"}),
        host=host,
        spec=spec,
    )
    # Session-level selection wins over the configured default.
    assert runtime._resolve_model({"model": "m-full"}) == "m-full"
    # Unknown ids fall back to the configured default.
    assert runtime._resolve_model({"model": "nope"}) == "m-fast"
    # No selection -> configured default.
    assert runtime._resolve_model(None) == "m-fast"


def test_codebuddy_argv_streaming_and_resume() -> None:
    fresh = codebuddy_argv("hi", None, "m-fast", None)
    assert fresh is not None
    joined = " ".join(fresh)
    assert "--output-format stream-json" in joined
    assert "--include-partial-messages" in joined
    assert "--model m-fast" in joined
    assert "--resume" not in joined

    resumed = codebuddy_argv("hi", None, None, "sess-1")
    assert resumed is not None
    joined = " ".join(resumed)
    assert "--resume sess-1" in joined
    assert "--resume-create-missing" in joined


# --------------------------------------------------------------------------
# Session registry persistence
# --------------------------------------------------------------------------


def test_session_registry_survives_restart(tmp_path: Any) -> None:
    kv_path = os.path.join(str(tmp_path), "kv.json")
    host1 = FakeHost(JsonKeyValueStore(kv_path))
    runtime1 = HeadlessCliRuntime(
        config=RuntimeConfig(runtime="fakecli", revision=1, values={}),
        host=host1,
        spec=make_spec(MCODE_STREAM),
    )

    async def first() -> str:
        await runtime1.start()
        await runtime1.create_and_start_session("s1", "go", client_message_id="cm-1")
        for _ in range(200):
            await asyncio.sleep(0.05)
            state = await runtime1.get_session_state("s1")
            if state is not None and state.status in ("idle", "error"):
                break
        await runtime1.stop()
        return runtime1._sessions["s1"].cli_session_id or ""

    native_id = asyncio.run(first())
    assert native_id

    # A fresh runtime instance (connector restart) restores the registry.
    host2 = FakeHost(JsonKeyValueStore(kv_path))
    runtime2 = HeadlessCliRuntime(
        config=RuntimeConfig(runtime="fakecli", revision=1, values={}),
        host=host2,
        spec=make_spec(MCODE_STREAM),
    )
    runtime2._load_persisted_sessions()
    sessions = asyncio.run(runtime2.list_sessions())
    assert [s.session_id for s in sessions] == ["s1"]
    assert runtime2._sessions["s1"].cli_session_id == native_id


# --------------------------------------------------------------------------
# Registration and protocol capability projection
# --------------------------------------------------------------------------


def test_default_providers_include_headless_cli_runtimes() -> None:
    runtimes = [provider.runtime for provider in default_runtime_providers()]

    assert "minimax" in runtimes
    assert "codebuddy" in runtimes


def test_capability_projection_keeps_headless_cli_runtimes() -> None:
    """Regression guard for connector.server.capabilities.

    A runtime missing from ``KNOWN_RUNTIME_CAPABILITY_IDS`` is projected to zero
    capabilities even though it is discovered, so the Server rejects every turn
    with "runtime capability is unavailable: session.send_message" while the
    runtime itself still answers when driven directly.
    """

    payload = protocol_capabilities_from_runtime_types(
        {
            "runtimeTypes": [
                {
                    "runtimeType": runtime,
                    "available": True,
                    "configSchema": {"schema": {"type": "object"}},
                    "capabilities": {"startTurn": True, "attachments": True},
                }
                for runtime in ("minimax", "codebuddy")
            ]
        }
    )
    projected = {(item["runtime"], item["capabilityId"]) for item in payload["capabilities"]}

    assert ("minimax", "session.send_message") in projected
    assert ("codebuddy", "session.send_message") in projected
    assert ("minimax", "runtime.attachment") in projected
    assert ("codebuddy", "runtime.attachment") in projected
    assert all(item["available"] is True for item in payload["capabilities"])


def test_session_capabilities_are_reported_for_existing_sessions(tmp_path: Any) -> None:
    """Regression guard for the session-scoped capability set.

    The Server fetches these facts before every reply, interrupt or catalog read
    on an existing session. Inheriting the base implementation's empty set marks
    all ten inherited ids unsupported, so a session could be created but never
    replied to ("session capability is unavailable: session.send_message").
    """

    kv_path = os.path.join(str(tmp_path), "kv.json")
    host = FakeHost(JsonKeyValueStore(kv_path))
    runtime = HeadlessCliRuntime(
        config=RuntimeConfig(runtime="fakecli", revision=1, values={}),
        host=host,
        spec=make_spec(MCODE_STREAM),
    )

    capabilities = asyncio.run(runtime.get_session_capabilities("s1"))

    assert capabilities.session_id == "s1"
    assert {c.capability_id for c in capabilities.capabilities} == {
        "session.send_message",
        "session.interrupt",
        "catalog.model",
        "runtime.attachment",
    }
    assert all(c.scope == "session" for c in capabilities.capabilities)
    assert all(
        c.supported and c.available and c.allowed for c in capabilities.capabilities
    )


def test_snapshot_never_claims_to_replace_the_stored_timeline(tmp_path: Any) -> None:
    """A complete snapshot makes the Server delete history missing from it.

    This kernel keeps its timeline in memory only, so claiming completeness would
    wipe the session's stored items on the first reconnect after a restart.
    """

    _host, _runtime, snapshot = asyncio.run(
        run_turn(str(tmp_path), make_spec(MCODE_STREAM))
    )

    assert snapshot.complete is False
