# Codex App and IDE coordination

The Connector can follow an existing Codex App/IDE conversation and expose its native state through Agents Anywhere (AA). This is an **opt-in** Codex runtime mode. The official `openai-codex` SDK remains the native backend for locally owned conversations; the coordination layer is a local peer protocol, not a replacement app-server JSON-RPC client. A wire handler, a native owner operation, a public AA control, and a visible Web control are different levels of support.

## Enablement and architecture

Set the Codex runtime's `appIntegration` field to `true` in its AA runtime configuration. Its schema revision is 8; the default is `false`, which keeps the existing SDK client path. Configure `codexHome` for the same user/home as the App (`CODEX_HOME` or `~/.codex` when left empty), then restart that runtime. The Connector creates a `CoordinatedCodexClient` around the official SDK and uses host-scoped runtime KV for its operation journal and queue. An explicit session view follows its source; background inventory reads do not retain every thread. At most 120 explicit views are retained by the runtime, leaving eight of the peer's 128 follow slots for transient reads; pinned active/owned/open-interaction views are not evicted. Capacity failure is explicit.

The endpoint is `<codexHome>/ipc/ipc.sock` on Unix. Endpoint directory and socket must belong to the current user and cannot be group/other writable; an elected router uses a private Unix socket. Windows named-pipe coordination is not implemented and reports `unsupported-platform`. App-owned threads stay owned by the App; AA sends supported follower requests to the exact current owner. AA-owned threads keep their official SDK native process and publish canonical state to App followers. Owner changes, disconnects, and resubscription invalidate stale response authority. The AA follower resnapshots on a patch revision gap, whereas the inspected IDE follower does not perform that gap recovery. Full history is a separate explicit read; a partial tail never authorizes deleting older AA timeline items.

The read-only probe uses a narrower connection mode: `CoordinationClient(start_router=False)`. It connects only to the configured endpoint, never elects a router or starts the SDK/native child. Follow/unfollow changes ephemeral subscriber bookkeeping and may advance the owner's stream revision when the first follower subscribes. It does not claim ownership or write a conversation, goal, settings, queue, journal, approval or native turn.

## Safe structural probe

From `connector/`, with the installed App already running and owning the specified thread:

```sh
uv run python -m scripts.probe_codex_coordination \
  --home /path/to/your/CODEX_HOME \
  --thread-id YOUR_EXISTING_THREAD_ID \
  --host-id local --duration 2
```

`--home` and `--thread-id` are required. `--host-id` defaults to `local`; use the actual coordination host ID for a nonlocal source. Duration defaults to two seconds and is limited to 0–30; connection/follow timeout defaults to three seconds and must be greater than zero and at most ten seconds (`--connect-timeout`). The probe initializes, discovers the owner, follows one thread, projects its canonical snapshot and later revisions with the production AA `CoordinationSnapshotProjector`, unfollows and closes in `finally`. It does not load complete history or invoke any follower write method. It prints JSON lines containing only IDs, role, normalized AA status, turn/item/request/timeline counts, completeness, and observed wire kind/version/revisions. Snapshot/patch wire version 11 is a **method version**, not the installed App binary version. No title, cwd, user/assistant text, goal objective, question, request payload, raw state or exception text is printed. A missing socket, inaccessible or unsafe endpoint, absent owner, follow timeout or projection failure exits nonzero with a bounded structural error code; it does not create/remove a socket or fall back to router election. A stream with no patch during the interval is a successful snapshot observation, not evidence that patches are unsupported.

The projection sink is in memory and counts timeline items rather than publishing private content to an AA server. This verifies the real AA state/timeline projector path, but does not establish browser rendering, server persistence, native write acceptance, or the installed App's version. For those, use the controller's separate live acceptance record and the App's own version evidence.

## Versioned coordination surface

Current local wire versions are exactly the following 23 `METHOD_VERSIONS` in `connector/runtimes/codex/coordination/wire.py`. “AA control” identifies a public runtime operation or command when present; “Web” describes implemented presentation/dispatch, not proof of installed-App UI parity. Owner operations still depend on current source authority, native version/feature policy, and native acknowledgements.

| Method (version) | Transport handler | Native owner operation | Public AA runtime | Visible Web surface |
| --- | --- | --- | --- | --- |
| `thread-stream-state-changed` (11) | Snapshot/patch apply; AA resnapshots on a gap. | Owner publishes canonical state. | State/timeline/goal/plan/notice projection. | Session timeline, goal/plan cards and notices. |
| `thread-stream-following-changed` (1) | Subscribe/unsubscribe broadcast. | Tracks current subscribers and sends snapshot. | Bounded explicit session view. | Views consume projected state; no follow button. |
| `thread-stream-following-status-requested` (1) | Re-follow after owner/reconnect request. | Owner asks for follower status. | Runtime lifecycle only. | No direct control. |
| `ipc-connection-reset` (1) | Synthetic local reconnect invalidation. | None. | Runtime recovery. | Recovered AA state/catalog; no direct control. |
| `thread-read-state-changed` (3) | Context-matched event. | None. | Event only; no general read-state mutation API. | No direct control. |
| `thread-archived` (2) | Lifecycle event. | None. | Source availability projection; no archive command here. | Availability display. |
| `thread-unarchived` (1) | Lifecycle event. | None. | Source availability projection; no unarchive command here. | Availability display. |
| `thread-owner-discovery` (1) | Host-scoped exact owner discovery. | Owner answers only for its claim. | Internal follow/operation prerequisite. | No owner-claim control. |
| `thread-follower-start-turn` (2) | Exact-owner request. | SDK native start with AA-owner journal identity. | Message/start. | Composer send; acceptance is not completion. |
| `thread-follower-load-complete-history` (1) | Owner request and revision wait. | Native pagination and complete publication. | `/history` (`refresh` alias), timeline sync. | Slash command and timeline. |
| `thread-follower-compact-thread` (1) | Exact-owner request. | Native compact. | `/compact`; accepted acknowledgement. | Slash command. |
| `thread-follower-steer-turn` (1) | Exact-owner request. | Native active turn steer with no retarget. | Steer. | Composer while running. |
| `thread-follower-interrupt-turn` (4) | Exact-owner request. | Physical interrupt; user Stop can pause goal/queue. | Session Stop versus turn interrupt. | Stop with partial/unknown result display. |
| `thread-follower-update-thread-settings` (2) | Exact-owner request. | Validated native settings, applied acknowledgement. | Selectors, `/settings`, `/plan on|off`. | Settings drawer and slash commands. |
| `thread-follower-update-daybreak` (1) | Exact-owner request. | Version/echo-gated native update. | `/daybreak` JSON command. | Slash command when enabled. |
| `thread-follower-edit-last-user-turn` (2) | Exact-owner request. | Last-turn revert/rollback and journaled restart. | `/edit` with turn ID/text. | Slash command; accepted is not completion. |
| `thread-follower-command-approval-decision` (1) | Exact pending request. | Native command approval response. | Authority-bound approval notice. | Approval action when writable. |
| `thread-follower-file-approval-decision` (1) | Exact pending request. | Native file approval response. | Authority-bound approval notice. | Approval action when writable. |
| `thread-follower-permissions-request-approval-response` (1) | Exact pending request. | Native permission grant from stored request. | Authority-bound permission notice. | Approval action when writable. |
| `thread-follower-submit-user-input` (1) | Exact pending request. | Native question response. | Input notice; native option mapping. | Form/plan-review action with opaque IDs. |
| `thread-follower-submit-mcp-server-elicitation-response` (1) | Exact pending request. | Form/URL response; special auth accept rejected. | Elicitation notice. | Form or safe URL, no fabricated auth response. |
| `thread-follower-set-queued-follow-ups-state` (1) | Exact-owner request. | Validates/persists one ordered native queue. | `/queue` JSON command. | Slash command; no dedicated queue editor. |
| `thread-queued-followups-changed` (2) | Owner queue broadcast. | Publishes queue state. | Queue observation. | No dedicated queue panel claimed. |

`initialize` and `client-status-changed` use version 0 and are outside this 23-method table. For request envelopes, `request_version()` uses the table version except that any `thread-follower-*` method with an explicit non-`None` envelope `hostId` uses table version **+1**. The local interrupt without `expectedTurnId` uses v3 instead of v4; the remote-host rule takes precedence and uses v5 for interrupt. Owner discovery carries `hostId` in **params**, while local follower owner requests omit the envelope `hostId` and nonlocal ones include it. Broadcasts use exactly the table version. No generic version negotiation establishes compatibility with arbitrary past/future App binaries; record the App/CLI version and observed wire versions for each live acceptance run.

## Native controls and support boundaries

Connector requires Python 3.12 and declares `openai-codex>=0.144.4`; that dependency floor is not an installed App protocol guarantee. Native binary selection checks a `codex`/`codex-cli` semantic version response before SDK launch. The current feature gates in `coordination/context.py` are revert >=0.148.0a13; turn trigger and per-turn service tier >=0.150.0a10; tool output >=0.151.0a4; reviewer >=0.153.0a4; daybreak >=0.154.0a6. Goal/review native controls additionally require >=0.155.1, an already AA-owned coordinated thread, a live raw-event bridge and observed goal support for goal mutations. Followed App/IDE goals are displayed but goal editing is disabled: no follower goal route exists. Installed 0.156.0 and 0.158.0-alpha.2.1 schemas were inspected during the implementation; schema presence alone does not prove a feature is enabled or accepted on every host.

Codex's current public command catalog contains status, compact, goal, review, model, reasoning, permission, plan, history, settings, daybreak, edit and queue. Selectors use the existing model/reasoning/permission UI; the other entries use exact grammar and disabled reasons from the runtime. `/settings` requires a JSON object with nonempty `threadSettings`; `/plan on|off` requires an authoritative latest native model and preserves known settings. `/queue` requires a JSON `state` containing only the current native thread ID and valid native message records; it may schedule work after acceptance. `/history` is a complete read and, for AA owner, may reconcile an unknown start only with positive matching native message identity; absence is not evidence that the previous start did not execute. Background inventory does not acquire ownership or hydrate all history. Unknown/rejected/accepted/completed command results remain distinct; Web retains drafts on uncertainty. DSH's command catalog is a separate dynamic native registry through the bridge, with attachment and installed-package limits of its own.

Unsupported IDE window/editor preparation, writing blocks, MCP model-context attachments, special MCP auth/verification acceptance, and background terminal/node REPL/subagent cleanup are not silently executed or acknowledged. A physical native Stop does not imply descendant/background cleanup. After timeout, disconnect or malformed acknowledgement, a write result may be unknown; neither Connector nor Web automatically resends it. The journal can reconcile locally observed client-message identity, but cannot observe a remote native-generation change or identical request reuse across that boundary; there is no global exactly-once guarantee. An installed native App UI cannot currently be automated through Codex CUA in this acceptance environment; purpose-built App operations and IPC observations must be described as such, not native UI automation.

## Verification and live evidence

For the structural probe and existing Connector coverage, from `connector/`:

```sh
env -u AGENT_SERVER_DB_URL -u AGENT_SERVER_DB_BACKEND \
  -u AGENT_SERVER_REDIS_URL -u DB_BACKEND -u REDIS_URL \
  uv run pytest tests/test_codex_coordination_probe.py -q
uv run ruff check scripts/probe_codex_coordination.py tests/test_codex_coordination_probe.py
```

The probe tests use temporary test-owned Unix sockets and real peer/projector code; the owner native backend is a fixture. Other Connector suites, native DSH bridge tests and mounted Web tests establish their own automated behavior, not installed-App or browser acceptance. The controller's ignored `.superpowers/sdd/2026-09-28-codex-ide-ipc/local-e2e-results.md` records live native/App IPC and API observations in progress; its final acceptance report will identify the exact commit, App/CLI/bridge versions, local service/browser evidence and unsupported or blocked cases. This page does not mark unfinished Chrome/UI scenarios as passed. Production deployment, installed-App replacement and main-branch merge are outside this work.
