
### API rewrite timeout

For a running Next server, all current AA API external rewrites share a bounded
100000ms socket timeout. This covers the existing running-session, no-attachment
send allocation: two 20s state reads, two 10s capability reads, a 30s message RPC,
and 10s transport/local margin. It does not change native or backend RPC limits,
and does not guarantee completion for runtime activation, attachments or database
contention. Future unrelated external rewrites must revisit this shared bound.

Set `AGENTS_ANYWHERE_API_PROXY_TIMEOUT_MS` to a positive integer from 1 through
2147483647 to explicitly override it (shorter values are honored). Otherwise,
passing `AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS=R` deliberately to the Next
process derives `ceil(1000 * (2 * R + 60))`; R must be finite and positive and the
result must fit the same timer range. Invalid explicit inputs fail configuration.
The backend and Next are separate processes: deployments with a nondefault read
budget must pass the same valid R to Next or specify a sufficient proxy override.
This does not change the backend's own environment parsing.

`NEXT_OUTPUT=export` ignores both proxy inputs and installs no proxy timeout or
rewrites. Static FastAPI deployments, packaged Electron and other upstream
proxies bypass this setting. API namespace normalization and destinations remain
unchanged. A proxy expiry can happen after successful native dispatch; never
retry a POST automatically or infer non-dispatch from a restored composer draft.
