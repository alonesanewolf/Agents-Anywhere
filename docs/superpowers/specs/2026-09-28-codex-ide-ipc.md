# Codex IDE-compatible collaboration for AA

## Intent and acceptance

Implement a fresh headless Codex coordination peer in the connector. The user
explicitly requested a worktree implementation and as much of the current IDE's
behavior as possible. App-running interoperability is the first acceptance target:
the same native thread id, live state, send/steer/stop and native approvals. Extend
that vertical slice to history, settings, queues and all current follower methods.
The existing official SDK remains the execution and inventory backend.

Do not read, copy, import or use old AA IPC implementations, reference tests, prior
protocol notes or deprecated code. Current active SDK/runtime interfaces are valid
integration surfaces. Source of truth is the installed OpenAI extension/App.

## Verified sources (2026-09-28)

- IDE: `openai.chatgpt-26.917.62051-darwin-arm64`, under
  `/Users/t4wefan/.vscode/extensions/`.
- IDE `out/extension.js`: transport, router, method versions, host bridge.
  SHA256 `7ba6208c447c393e050a8ba46893e9e1aa4abd718cb4a8610fa87b12942633bc`.
- IDE `webview/assets/app-initial-113eb9b2c1a2.js`: owner/follower state,
  revisions and operation semantics. SHA256
  `acf372e1e1c64679915cad75014f766a1604004955eb8068c718e6e1f62d0b9e`.
- IDE `webview/assets/app-initial-e47a6bae212d.js`: coordination bridge.
- App `/Applications/ChatGPT.app`, version 26.924.22138 (11645).
- A read-only live probe received an owner discovery response, a v11 snapshot and
  consecutive patches from the running App. Snapshot history uses
  `turnHistory.kind=canonical`; top-level `turns` may be empty. No live mutations
  have been tested yet. Do not store private conversation text as fixtures.

## Wire contract

Frames are four-byte little-endian unsigned length plus UTF-8 JSON, max 256 MiB.
This is NOT the app-server JSON-RPC protocol. Main Unix endpoint is
`$CODEX_HOME/ipc/ipc.sock`; legacy fallback is `/tmp/codex-ipc/ipc-<uid>.sock`.
Windows uses `\\.\pipe\codex-ipc`. Secure endpoint ownership and avoid unlinking
active or foreign sockets. Client type is honestly `agents-anywhere`.

Request: `{type:request, requestId, sourceClientId, version, method, params,
targetClientId?, hostId?, timeoutMs?}`. Initialization uses source
`initializing-client`, method `initialize`, v0, params `{clientType}`. Response:
`{type:response, requestId, resultType:success, method, handledByClientId, result}`
or `{type:response, requestId, resultType:error, error:<string>}`. Initialization
result is `{clientId}`.

Broadcast: `{type:broadcast, sourceClientId, method, version, params,
targetClientIds?}`. Router excludes the sender and authenticates sourceClientId.
Discovery: `{type:client-discovery-request, requestId, request:<original>}` and
`{type:client-discovery-response, requestId, response:{canHandle:<bool>}}`.
Targeted requests also require discovery. Register request handlers separately
from their can-handle predicates. Default request timeout 5s; discovery 10s;
reconnect delay 1s. Dispose rejects waiters and cancels all tasks.

Method versions:

| Method | Version |
| --- | ---: |
| thread-stream-state-changed | 11 |
| thread-stream-following-changed | 1 |
| thread-stream-following-status-requested | 1 |
| ipc-connection-reset | 1 |
| thread-read-state-changed | 3 |
| thread-archived | 2 |
| thread-unarchived | 1 |
| thread-owner-discovery | 1 |
| thread-follower-start-turn | 2 |
| thread-follower-load-complete-history | 1 |
| thread-follower-compact-thread | 1 |
| thread-follower-steer-turn | 1 |
| thread-follower-interrupt-turn | 4 |
| thread-follower-update-thread-settings | 2 |
| thread-follower-update-daybreak | 1 |
| thread-follower-edit-last-user-turn | 2 |
| thread-follower-command-approval-decision | 1 |
| thread-follower-file-approval-decision | 1 |
| thread-follower-permissions-request-approval-response | 1 |
| thread-follower-submit-user-input | 1 |
| thread-follower-submit-mcp-server-elicitation-response | 1 |
| thread-follower-set-queued-follow-ups-state | 1 |
| thread-queued-followups-changed | 2 |

Unknown methods default v0. Local follower requests omit outer hostId; a remote
hostId raises the version by one. Interrupt without expectedTurnId uses v3 locally.
The request version matcher also accepts that legacy interrupt form.

## State and ownership

Ownership is per (hostId, conversationId), independent of router election. Owner
discovery returns `{supportsUntrustedAppInput:true}` only when the owner can safely
support responseItems; otherwise advertise false. Do not silently lose context.
Following subscribes to the owner's canonical state. Repeated follow resends a
snapshot. Preserve unknown fields, request ids/types, native item ids and canonical
history; AA timeline is a derived representation, never the authoritative store.

State broadcast params: `{conversationId,hostId,change}`. Snapshot change is
`{type:snapshot,revision,conversationState}`; patches are Immer array paths with
`{type:patches,baseRevision,revision,patches,acceptedTextChanges?}`. Apply a batch
atomically, only from the current owner and matching revision. Canonical history
islands contain entries whose `value` indexes `entitiesByKey` (verify exact shapes
in installed bundle/live sanitized structure). Preserve partial-history status.
On a gap AA deliberately requests a new snapshot; the inspected IDE merely ignores
the gap. This recovery extension must be documented, not attributed to the IDE.

Never publish an IPC-derived state back as a native update or let SDK events
overwrite a followed conversation. Reconnect restores follows and ownership;
owner loss invalidates state/revision waiters. No automatic duplicate send after
timeout/connection failure with unknown result. Native fallback is permitted only
after confirmed no owner, with a fresh discovery before mutation.

## Operations and integration

Use an opt-in `appIntegration` provider config field (false by default while this
is experimental), preserving the existing SDK-only path. Capability reporting
must distinguish supported operations from connection health and unverified native
version/platform support. Keep the connector headless and generic AA UI usable.

Provide all 14 follower method routes with exact params/results. Owner side checks
ownership immediately before mutation. Implement execution via the official SDK,
not a second handwritten app-server transport. Preserve extended turn input and
context or reject unsupported context explicitly. Never auto-approve requests.

Start wraps `{conversationId,turnStart:{request,context}}`, returns `{result}`.
Steer includes input and clientUserMessageId. Interrupt uses expectedTurnId and
mode, must not stop a newer turn; pause active goals as appropriate. Full-history
returns `{revision}` and awaits observation of that revision (30s waiter,
300s owner / 305s follower request limit). Compact uses native compact.
Settings serialize per thread and honor ifModelEquals/ifEffortEquals; acknowledge
`applied` only after success. Edit-last validates latest user turn and idle status
before native rollback/restart. Daybreak is explicit state; don't invent semantics.
Command/file approval, permissions, questions and MCP elicitation use the exact
outstanding native request id/type. Require pending matching request before respond,
never route a stale approval to a new owner with a reused id.

Queue state is owner-controlled, serializable, persisted in connector runtime KV,
broadcast on acceptance; only the owner executes it. Respect idle/send locks,
ordering, pausedReason and unknown outcomes. Archive/unarchive/read-state/context
notifications preserve host/account scope. IDE UI-specific features with no AA
equivalent remain explicit unsupported operations or retained protocol state.

## Validation and delivery

Meaningful TDD tests use real local sockets and independent clients, literal
protocol fixtures, synthetic canonical histories and fake execution backends. Test
fragmentation, routing, ownership, revision gaps, live patch projection, reconnect,
ambiguous failures, exact controls and approvals. Existing Codex tests must pass.
Read-only live test uses the new implementation against the installed App, prints
only counts/status; no private text or secrets in artifacts. Live write tests need
an isolated test conversation and must never interrupt this active task.

Commit milestones on `codex/codex-ide-ipc`, preserve the user's original checkout.
Document source/version matrix, activation, tests, limitations, and verified versus
unverified behavior. Do not claim full parity merely because a wire route exists.
