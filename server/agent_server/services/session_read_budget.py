"""Read-only session RPC budgets; mutation deadlines are independent."""

import os

CODEX_SESSION_READ_TIMEOUT_SECONDS = 20.0


def session_read_timeout_seconds(runtime: str | None = None) -> float:
    # Explicit overrides (including shorter test/deployment bounds) keep priority.
    configured = os.environ.get("AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS")
    if configured is not None:
        try:
            return float(configured)
        except ValueError:
            return 10.0
    # 17s shared connector preparation + 3s caller/transport margin. This does
    # not guarantee service under overload and never authorizes dispatch on expiry.
    return CODEX_SESSION_READ_TIMEOUT_SECONDS if runtime == "codex" else 10.0
