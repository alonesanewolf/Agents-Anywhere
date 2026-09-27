# Codex IDE IPC implementation plan

> Execute in the existing worktree; user has approved implementation. Use the
> subagent-driven-development skill with one implementer at a time, task reviews,
> and a final whole-branch review. Independent source inspection may run alongside.

**Spec:** `docs/superpowers/specs/2026-09-28-codex-ide-ipc.md`
**Base:** `17d7bc35` from origin/main.
**Worktree:** `/Users/t4wefan/.codex/worktrees/codex-ide-ipc/Agents-Anywhere`

## Global constraints

- Do not read, copy, import or use old AA IPC implementations, reference tests,
  prior protocol notes or deprecated code. Use the installed IDE/App and the spec.
- Keep all code changes in this worktree. The user has now explicitly authorized
  an isolated local AA service stack and computer-controlled end-to-end testing
  after implementation. Use dedicated test conversations and test data; preserve
  unrelated running services and conversations. No production deployment or main
  merge. Native app replacement/release remains outside this task.
- Python 3.12, uv, asyncio, modular/headless implementation. Official SDK remains
  native backend; no handwritten app-server JSON-RPC transport.
- Write meaningful failing behavior tests before implementation. Check target
  tests and ruff; preserve unrelated behavior. Commit each verified milestone.
- Preserve unknown state/input fields. Never duplicate a send after an ambiguous
  timeout or acknowledge unsupported operations as successful. No auto-approval.
- One implementer owns its listed files at a time. Review before dependent tasks.

### Task 1: IPC wire, client and secure router

**Files:** new `connector/connector/runtimes/codex/coordination/{__init__,wire,transport,router}.py`,
`connector/tests/test_codex_coordination_transport.py`.

- [ ] Read the spec's wire contract and inspect the installed `out/extension.js`
  for exact envelopes, version helpers, framing, endpoint checks and initialization.
- [ ] Define `IpcError(code)`; method-version helpers; framed encode/read with max
  256 MiB and reject malformed/oversized data before allocation. Do not log payloads.
- [ ] Expose `CoordinationClient(codex_home, *, endpoint=None, client_type='agents-anywhere',
  start_router=True)`; async `start()`, `close()`, `wait_initialized(timeout=5)`,
  property `client_id`, `request(method,params,*,target_client_id=None,host_id=None,
  timeout=5)` returning the FULL success response, `broadcast(method,params,*,
  target_client_ids=None)`, `add_request_handler(method,can_handle,handler)` and
  `add_broadcast_handler(handler)` returning remove callbacks. Handler receives
  the full envelope, predicate may be async, broadcast handlers async.
- [ ] Implement router election for Unix safely (ownership, parent permissions,
  no symlink/regular file unlink, connection test, EADDRINUSE race). Provide an
  explicit unsupported-platform error if Windows cannot be implemented/tested;
  isolate stream open/listen for future named-pipe support.
- [ ] Router discovery/routing/broadcasts authenticate source peer and response
  sender; timeouts/disconnect cleanup. Initialize generated ids, status broadcasts,
  reconnect with synthetic self-connected/reset events. All background tasks,
  pending requests/waiters and owned socket cleanup have deterministic shutdown.
- [ ] Write socket tests for fragmented/coalesced frames, out-of-order request
  replies, targeted and untargeted discovery, unsupported versions, sender exclusion,
  reconnect, pending failures, dishonest responses, unsafe endpoint and teardown.
- [ ] Run `uv run pytest tests/test_codex_coordination_transport.py -q` and ruff on
  touched Python. Commit. Report exact tests and evidence in assigned report file.

### Task 2: Canonical conversation state and owner/follower peer

**Files:** new `coordination/{state,peer}.py`,
`connector/tests/test_codex_coordination_state.py`,
`connector/tests/test_codex_coordination_peer.py` (paths under connector as above).

- [ ] Inspect current IDE state reducer and coordination bridge; read spec and
  Task 1 public API. Define pure state helpers, atomic Immer patch application,
  canonical turn enumeration retaining unknown fields, revision/role tracking.
- [ ] Expose `CoordinationPeer(client, *, host_id='local', on_state=None,
  owner_handler=None)`; start/close, follow/unfollow/discover_owner, request_owner,
  get_state, is_owner/is_follower, claim/release, publish_state, wait_revision.
  State callback gets thread id + full canonical state; owner handler gets full
  method/params and returns exact wire result. Coordinate signatures with controller.
- [ ] Implement all follower request registrations, strict host/owner predicates,
  snapshots/patches/targeted resync, status following, owner loss and reconnect.
  Request full history and await its revision. No mutate retries on unknown outcome.
- [ ] Include queue state and metadata broadcasts as explicit peer events; read
  state only with host/account context match. Forward unknown context verbatim.
- [ ] Tests use multiple real Task 1 clients, synthetic canonical history and
  literal wire fixtures. Cover ownership, repeated follow, gaps/bad patch rollback,
  reconnect, revision waiters, caller timeout, host isolation and queue sync.
- [ ] Run focused tests + transport regression + ruff, commit and report.

### Task 3: Official SDK coordination adapter and native owner operations

**Files:** new `coordination/{client,projection,operations}.py`; extend active
`sdk/{client,runtime_client}.py` as needed; new adapter/operation tests.

- [ ] Add a `CoordinatedCodexClient` implementing existing `CodexRuntimeClient`,
  wrapping SDK client + peer, keeping native inventory/catalog and same thread ids.
  Reads follow owners and project their canonical state; sends/steer/stop/compact/
  responses go to discovered owner. Native events never overwrite follower state.
- [ ] Preserve full raw canonical state in peer; derive native-shaped thread and
  notification data for AA. Handle canonical islands, partial/completed turns,
  item deltas, approvals/questions/elicitation, request resolution, active status,
  settings/token usage. Owner state must be compatible with current IDE snapshots.
- [ ] Implement all current follower actions on SDK-owned conversations, using
  official SDK extensible request mechanism for missing high-level methods. Safely
  validate operation context, current ownership and exact outstanding request type.
  Support full history, serialized conditional settings, daybreak, last-user edit,
  approvals, questions, elicitation, queue acceptance/execution/persistence.
- [ ] Explicitly feature-gate unsupported native methods/context. A native version
  rejection remains visible; no false ok. Preserve result shapes and unknown input.
  Isolate queue/settings locks; never block the socket reader awaiting own state.
- [ ] Tests cover App-owned and AA-owned round trips with fake SDK and real peers,
  no duplicate native mutation, stop stale id, full history revision, conditional
  settings, queue ordering/failure, exact request ids and stale approval rejection.
- [ ] Run new tests + existing SDK tests + ruff; commit and report limitations.

### Task 4: AA provider, runtime controls and end-to-end integration

**Files:** active `provider.py`, `provider_config.py`, `runtime.py`, relevant
`domain/`, `turns/`, `notifications/` only as necessary; a default-noop runtime
session-view hook, instance binding and explicit session-read RPC forwarding;
provider/runtime and focused subscription-seam tests.

- [ ] Add opt-in `appIntegration` config schema/default/normalization revision;
  instantiate adapter with configured home and runtime KV. Default SDK path remains.
- [ ] Wire full-state change notifications into timeline/state/notices without
  losing canonical pagination status or publishing duplicate items. Preserve
  client-message reconciliation, model/permission selections and capabilities.
- [ ] Make history, compact, queue, edit, settings/daybreak and supported metadata
  operations reachable through AA RuntimeCommand/interaction APIs. Do not advertise
  unsupported features as usable; preserve existing generic protocol contracts.
- [ ] Update architecture tests only where SDK-only prohibition conflicts with
  authorized integration, retaining bans on old/reference imports and duplicate
  raw app-server transports. Never read old tests as implementation guidance.
- [ ] Test provider enable/disable, AA host receiving live owner output and notices,
  send/steer/interrupt/respond through existing runtime calls, reconnect resync and
  SDK-only regressions. Run all active Codex + architecture tests; commit.

### Task 5: Live validation, local service and UI end-to-end acceptance

**Files:** `connector/scripts/probe_codex_coordination.py`,
`connector/docs/codex-app-integration.md`, connector README activation link,
source/test fixes identified by review.

- [ ] Add opt-in read-only probe using the production implementation. Accept home
  and thread id; discover/follow, print ids/status/counts/revisions only, unsubscribe
  and close. No raw text, no sends/approvals, no socket unlink or router election.
- [ ] Run probe against installed running App; confirm observed snapshots/patches
  exercise AA projection, record version and structural evidence. No live mutation
  of active task. Clearly identify write-side verification level.
- [ ] Start an isolated local AA stack from this worktree using separate ports
  and disposable test data. Use computer control to test the actual AA interface.
  Exercise App-owned and AA-owned dedicated conversations, bidirectional output,
  sends/steer/interrupt, approvals/questions, reconnect, slash commands and
  goal/plan presentation. Fix observed failures and rerun the affected scenario.
  Record evidence and distinguish live-tested, fixture-tested and unsupported
  behavior. Never mark this goal complete before the UI acceptance work is done.
- [ ] For DSH, run the new bridge in a local development/test host or another
  supported isolated loading mechanism to validate native commands. Do not claim
  the installed desktop package gained the routes without an actual package
  update. Any native App UI access restriction must be reported explicitly.
- [ ] Document all 23 versioned methods, source versions, supported operations,
  enablement and test commands, architecture, resync difference and platform/native
  feature limits. A wire handler alone does not count as full UI feature parity.
- [ ] Run meaningful complete regression once changes settle; collect branch diff
  for independent final review and fix material findings. Commit final state.
  Push this new feature branch when verified; no main merge or deployment.

### Task 6: Codex and DSH slash commands; goal and plan presentation

**Added by user steering:** implement slash commands for both runtimes in AA;
evaluate how Codex goals and plans should be presented, including a UI component
when existing presentation cannot expose their persistent state and actions.

**Files:** active Codex/DSH command catalog and execution adapters, runtime
capabilities, relevant connector tests; existing web-next session composer and
session display components with tests when behavior changes; UI design note.

- [ ] Inspect active DSH native command API and installed Codex IDE commands. Map
  command names, arguments, enablement, scope and responses into RuntimeCommand.
  UI-only commands need AA equivalents or explicit unsupported reasons, never
  send them as accidental prompt text or acknowledge them as executed.
- [ ] Reuse AA's existing slash menu, fixing argument entry and command results
  where needed. Runtime capabilities and disabled reasons reflect availability.
- [ ] Implement supported native actions for both runtimes and cover catalog to
  execution through public runtime interfaces; add parsing/menu behavior tests
  where the existing composer needs corrections.
- [ ] Inspect native goal and plan event shapes and AA timeline capabilities.
  Present a concrete design in a short repository note. Reuse plan checklist if
  present. If persistent goals require a component, implement a small card with
  objective/status/budget/progress and only actions supported by native methods.
  Preserve unknown/pending states and never invent progress percentages.
- [ ] Read web-next/AGENTS.md and installed local Next documentation before web
  edits; use existing framework/components. No dev server or heavy local build.
- [ ] Run focused connector and UI tests/type checks, review, commit. Final task
  order puts this before Task 5 final audit so the complete branch is reviewed.
