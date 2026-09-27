"""Public Python command adapter against the test-owned compiled native Host."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from connector.runtime_protocol import RuntimeConfig
from connector.runtimes.dsh.bridge.client import BridgeClient
from connector.runtimes.dsh.discovery import load_endpoint
from connector.runtimes.dsh.runtime import DshRuntime
from connector.runtimes.session_identity import stable_runtime_session_id


async def main(home: Path) -> None:
    async def ignore(*_args: object) -> None:
        return None

    client = BridgeClient(
        endpoint=load_endpoint({"dshHome": str(home)}), connector_id="commands-probe",
        session_namespace="wire", client_version="test", startup_timeout=5, request_timeout=5,
        notification_handler=ignore, exit_handler=ignore,
    )
    runtime = DshRuntime(RuntimeConfig("dsh", 2), SimpleNamespace(connector_id="commands-probe"))
    await client.start()
    runtime._client = client
    session = stable_runtime_session_id("wire", "dsh", "commands-wire")
    try:
        catalog = await runtime.list_commands(session, "commands-wire", query="echo", limit=1)
        assert len(catalog) == 1 and catalog[0].id == "echo"
        assert catalog[0].metadata["ui"]["acceptsMultiline"] is True
        result = await runtime.execute_command(session, "echo", "commands-wire", raw="/echo  first\nsecond  ")
        assert result.ok and result.message == "  first\nsecond  "
        assert result.result["executionState"] == "accepted"
        assert result.result["commandId"] and result.result["sourceEventSeq"] >= 0
        mismatch = await runtime.execute_command(session, "echo", "commands-wire", raw="/permission workspace-write")
        assert not mismatch.ok and mismatch.code == "invalid_command"
        unknown = await runtime.execute_command(session, "absent", "commands-wire", raw="/absent")
        assert not unknown.ok and unknown.code == "unknown_command"
        error = await runtime.execute_command(session, "permission", "commands-wire", raw="/permission missing")
        assert not error.ok and error.result["kind"] == "error" and error.result["executionState"] == "completed"
        permission = await runtime.execute_command(session, "permission", "commands-wire", raw="/permission workspace-write")
        assert permission.ok
        client.request_timeout = 0.05
        timed_out = await runtime.execute_command(session, "wait", "commands-wire", raw="/wait")
        assert not timed_out.ok and timed_out.code == "command_outcome_unknown"
        assert timed_out.result == {"executionState": "unknown", "retryable": False}
        assert (await client.request("ping"))["ok"]
        print("DSH compiled native command integration passed")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
