# Task 6a — DSH native command implementation

DONE for the active DSH bridge and Python adapter slice, 2026-09-28.

Base: `c9f8faad4decc6bea5ad86a7b16bb5c815915c9b`.
Implementation: `7caad61a` (`feat(dsh): bridge native command registry and execution`).
Compiled native/Python verification: `1aabc89d` (`test(dsh): verify compiled native command transport through Python`).
No push, release, installed-App changes, development-service startup or production actions. Controller still owns independent review and installed-native/UI acceptance.

## Implemented behavior

- `RuntimeCommands` in the current bridge owns dynamic scoped catalog mapping and invocation, using the installed native `@deepseek-ai/dsh-commands` 0.1.7-rc.2. No optional command inventory is hardcoded. Global and Agent-scoped definitions are listed afresh; native permission handling is exercised through the real package.
- `RuntimeRouter` exposes `session.listCommands` / `session.executeCommand`, retaining existing namespace/identity/source validation. Agent resolution uses `RuntimeConfiguration.agent` → real `sessionController.resolveAgent`, checks returned identity and signal, and rechecks source availability before admission. Neither route calls `prompt` or creates a session.
- Exact supplied raw text wins, including empty strings, spacing and newlines. Public native `parseCommand` enforces requested name agreement. Zero raw-absent args yields `/name`; one string yields `/name ` plus that string unchanged; multiple args require raw. Non-string/malformed args, mismatches and explicit nonempty/malformed attachments reject before a native command handler runs.
- Native descriptors preserve `input`/`definitionId`, `acceptsArgs`, hint and attachment affordance, while explicitly declaring AA command attachments unavailable. Native handlers own invocation-specific and busy-state validation; no idle-only gate is introduced.
- Normal native success/error preserve commandId, kind, text and available sourceEventSeq. Success defaults to accepted; returned error is a completed failed handler. Throws/abort do not yield correlation, and AA never guesses one from concurrent lifecycle events.
- `commands/change()` invalidates the opaque catalog revision and publishes existing runtime/session capability events. Actual registry/controller availability is required; session capability also requires source availability and authoritative Agent resolution. Registry unload removes support. Revisions differ on adapter restart and registry register/dispose.
- Python `DshRuntime` now implements public `list_commands` and `execute_command`; list query/limit and exact raw/args are transported and existing models decode the public descriptors/results. Missing, partial or unsupported old-bridge capabilities stay unavailable with an upgrade reason and no command RPC. New-bridge source/service failure remains a distinct unavailable state. Provider command support requires all reported support/availability/authorization flags.
- Execute makes exactly one mutating RPC. Timeout, disconnect, native request cancellation and malformed/mismatched response correlation cannot cause reconnect/replay; uncertain results expose unknown + retryable:false. Explicit Python cancellation propagates and existing BridgeClient sends `$/cancelRequest`.
- Existing active `TECHNICAL.md` documents the command routes and consumer behavior.

## Shared consumer interface (Web6c / server6b)

Requests on the private bridge:

```ts
session.listCommands({ sessionId, externalSessionId?, query?, limit? })
// limit default 50, integer 1..1000, filter before limit
session.executeCommand({ sessionId, externalSessionId?, command, raw?, args? })
// command = bare native name, not definitionId or execution commandId
// Bridge also explicitly rejects nonempty attachments if directly supplied.
```

List returns `{commands:[...]}` with existing RuntimeCommand fields. Descriptor metadata is:

```ts
{
  definitionId?: string,
  input?: { hint: string, attachments?: boolean }, // exact native descriptor
  attachmentsAvailable: false,
  ui: {
    kind: 'execute', argumentHint?: string, acceptsMultiline: true,
    allowedStatuses: ['idle','running','waiting','pending','stopping','waiting_approval','error','blocked']
  }
}
```

Presence of native input sets `acceptsArgs:true` and `argsSchema:{type:'string',description:hint}`. UI uses acceptsArgs for editable insertion; no required-argument inference from hint text. allowedStatuses delegates online busy-state handling to native validation; UI must still enforce source/read-only/write authorization and pending-submit guards.

Existing capability row `session.commands.metadata.catalogRevision:string` is opaque (`adapter UUID:counter`). Bridge sync emits `runtime.capability.updated` and `session.capability.updated`; Python maps both to platform `runtime.capability.updated` with the capability-set identities and optional sessionId/runtimeId. Web6c refreshes the open menu on relevant catalogRevision/availability change, and on reopen, reconnect or session/runtime switch. No new event type or schema top-level field was added.

Execution response:

```ts
{
  command: string, ok: boolean, code?: string, message?: string,
  result: {
    commandId?: string, kind?: 'success'|'error', text?: string,
    sourceEventSeq?: number,
    executionState?: 'accepted'|'completed'|'unknown', retryable?: false
  }
}
```

- Success: `ok:true`, exact native message/correlation, `executionState:'accepted'`.
- Native returned error: `ok:false,code:'command_error'`, exact native text/correlation, `executionState:'completed'`; no rollback claim.
- Validation/name miss: `invalid_command`, `command_attachments_unsupported` or `unknown_command`, `ok:false`, no executionState/correlation.
- Old bridge: `bridge_upgrade_required`; current source/service unavailable: `commands_unavailable`; no execution RPC in either case.
- Thrown handler: `command_failed`, `ok:false`, `{executionState:'unknown',retryable:false}` and sanitized refresh-first message.
- Timeout/cancelled dispatch/transport loss: `command_outcome_unknown`, `ok:false`, same unknown/no-retry result. Server6b should preserve/classify any outer timeout consistently and never resubmit the mutation.

Web must keep the failed draft and treat ok:false as failure regardless of HTTP status. Accepted is an acknowledgement, not evidence of resulting model/background completion. An uncooperative native handler can continue side effects after abort; unknown does not mean non-execution. Native lifecycle records settle errors but correlation is unavailable in thrown/aborted responses.

## Meaningful red / green evidence

Before production implementation, source-native tests failed for four missing command routes (`METHOD_NOT_FOUND`) and unsupported `session.commands`. An initial fixture setup miss (missing native Typert/Gateway composition) was repaired before capturing this behavioral RED; missing lib output was not used as RED.

Before Python overrides, public tests failed on empty catalog, generic old-bridge reason and inherited unsupported execution. Additional behavior tests caught partial capability accidentally enabling commands, wrong command identity being reported successful, and native error kind being reported successful. Native edge tests caught the absence of error-status availability and null args being accepted. Each was fixed and rerun to GREEN.

Native tests compose actual Cordis Commands, SessionStore/persistence/query, Agent/AgentLoop, presets and Session Controller. The command fixture registers no model adapter; tests do not mock commands.list/execute or controller resolution. Global/scoped registry contributions are registered through the real native registry; the existing real permission command verifies actual native semantics. Assertions confirm no ordinary turn starts and no extra user prompt is recorded. Test-only registry handlers exercise raw input/correlation and thrown/uncooperative cancellation edges.

The joined compiled integration loads real `lib/index.js` using the active nativeRuntime fixture into a test-owned home, then invokes the real Python `DshRuntime` public command methods over authenticated BridgeClient TCP transport. It verifies actual native registry/controller/permission behavior, query/limit, multiline exact raw, success/source correlation, identity rejection, unknown names, native error and timeout with exactly one command/run and no ordinary turn. This is separate from installed desktop/CLI acceptance.

## Executed verification

All test commands below were run with AGENT_SERVER_DB_URL, AGENT_SERVER_DB_BACKEND, AGENT_SERVER_REDIS_URL, DB_BACKEND and REDIS_URL explicitly unset (`env -u ...`).

- In `dsh-bridge-next`: `corepack yarn exec tsx --test tests/integration/runtime-commands.test.ts tests/integration/runtime-configuration.test.ts tests/integration/runtime-events.test.ts tests/integration/runtime-state.test.ts tests/integration/runtime-ownership.test.ts tests/integration/runtime-effort-capability.test.ts` → **34 passed, 0 failed** (includes 7 command tests; joined actual Python/native integration passed).
- In `connector`: `uv run pytest tests/test_dsh_commands.py tests/test_dsh_provider.py tests/test_dsh_bridge_client.py tests/test_dsh_event_sync.py -q` → **42 passed** (12 new command cases).
- `corepack yarn typecheck` → exit 0.
- Exactly one authorized `corepack yarn build` → exit 0; real Host/client output and internal Connector source bundle are available for controller E2E. Existing lucide-react `use client` bundling warnings appeared; no build failure.
- `corepack yarn check:build` → exit 0, Host/client/declaration/Connector artifact checks passed.
- `uv run ruff check --ignore TRY004,BLE001 connector/runtimes/dsh/runtime.py connector/runtimes/dsh/provider_config.py connector/runtimes/dsh/bridge/models.py tests/test_dsh_commands.py tests/dsh_commands_probe.py` → all checks passed.
- `git diff --check` → exit 0; owned verified milestones committed, no push.

Plain focused Ruff still reports **five existing baseline violations**: three TRY004 in models.py and two BLE001 in runtime.py. These were reproduced against the exact BASE source using `git show BASE:path | uv run ruff check --stdin-filename ... -`; the BASE runtime also had I001 import order, which the touched import block now fixes. No new lint violations were suppressed in source.

The controller-documented optional `@dataiku/uv-darwin-arm64` X_OK/mode-0644 unit failure was not altered or hidden. No node_modules chmod occurred. The full unrelated package suite was not rerun; the relevant 34-test native suite and build/artifact checks passed without that baseline issue obstructing command validation.

## Remaining boundaries

Actual installed native/UI acceptance, package deployment and any DSH release are controller work. Optional goal/compact/plan/feedback plugins are advertised only when the live host registry contains them; this adapter does not recreate their native semantics. Command attachments remain explicitly unavailable through the current public AA API. Generic native successes remain accepted because arbitrary registry handlers may start asynchronous/model work. Shared server query/limit forwarding and outer ambiguous-timeout handling remain Task6b; Web argument editing, menu invalidation, failure draft preservation and goal/plan presentation remain Web6c.
