# Runtime commands, goals and plans

This extends the user-approved Codex IPC implementation with the user's request
for Codex and DSH slash commands and a considered goal/plan presentation.

## Sources and verified constraints

Current Codex IDE extension 26.917.62051, its webview bundles, and the official
Python SDK 0.155.1 were inspected directly. DSH's current command registry types
and enabled command packages were inspected in its vendored runtime 0.1.7-rc.2.
The current AA connector, next bridge and web composer are integration surfaces.
No earlier AA IPC implementation informs this design.

The installed IDE's slash menu is a mixture of native operations and client UI
actions. There is no generic Codex execute-slash RPC. Map each supported command
to its actual operation. DSH has a real native command registry, but AA's next
bridge currently lacks command-list/execute routes and marks session.commands
unsupported. This branch must add the bridge routes as well as Python adapters.

## Commands

Reuse RuntimeCommand, the existing session command API and the AA composer menu.
Keep descriptions, aliases, argument hints, native availability and disabled
reasons. Preserve the exact raw line for DSH; splitting and joining whitespace
changes native command input. An argument-taking menu item inserts an editable
command instead of immediately executing an empty invocation. Keep the draft on
failure. Treat a successful HTTP response with ok:false as a command failure.
Unknown/multiline slash input must not accidentally become a model prompt.

DSH registry contract:

```ts
commands.list(agent): readonly CommandDescriptor[]
commands.execute(agent, line, attachments, signal): Promise<CommandExecution | undefined>
// Descriptor: {definitionId?, name, description, input?: {hint, attachments?}}
// Execution: {commandId, result: {kind: 'success' | 'error', text?, sourceEventSeq?}}
```

Resolve the authoritative native Agent through the bridge's existing session
controller. Respect native command overrides and commands/change invalidation.
Unknown commands return a visible error without starting model work. Preserve
commandId and sourceEventSeq in the public result. Add capability detection, so
an older bridge remains explicitly unavailable until upgraded; do not merely
change commands:false to true. This branch does not authorize a DSH release or
replacement of an installed desktop app.

Verified native DSH commands are goal, compact, feedback, permission and plan.
Optional commands must come from the live registry, not a hardcoded assumption.
DSH model is a client selector; use AA's existing model control. For the current
attachment-free command API, reject attachments explicitly until there is a
complete attachment transport; never silently discard them.

Codex compact maps to thread/compact/start and can forward to an App owner. Plan
mode maps to collaborationMode settings; model/reasoning map to the existing
selection controls or supported settings flow. Goal and review require native
owner operations; exclude or disable them where the current owner route cannot
perform them. Product/navigation commands such as project/new/side require an AA
equivalent before being advertised. Native acknowledgements must not be presented
as completion of a background operation.

## Persistent goals

Introduce a compact SessionGoalPanel near session status/composer. It presents
native objective and status, plus actual usage when available. Collapse completed
goals to a concise summary. Do not derive goal completion from physical turn end,
model text or token usage percentage.

Codex native shape:

```ts
type ThreadGoal = {
  threadId: string; objective: string;
  status: 'active' | 'paused' | 'blocked' | 'usageLimited' | 'budgetLimited' | 'complete';
  createdAt: number; updatedAt: number;
  timeUsedSeconds: number; tokensUsed: number; tokenBudget?: number | null;
};
// thread/goal/get {threadId} -> {goal: ThreadGoal | null}
// thread/goal/set {threadId, objective?, status?, tokenBudget?} -> {goal: ThreadGoal}
// thread/goal/clear {threadId} -> {cleared: boolean}
// thread/goal/updated {threadId, turnId?, goal}; thread/goal/cleared {threadId}
```

Hydrate goal on attach/reconnect. IPC state may expose threadGoal and
completedThreadGoal. Retain the latter when App clears the current completed goal.
Do not copy App auto-clear behavior without preserving completion visibility.
Never drop tokenBudget through a convenience SDK method that omits it.

The current IPC follower method set has no goal mutation route. The installed IDE
goal creation path explicitly requires owner role. Therefore App/IDE-owned goals
are read-only in AA with this protocol; do not resume the thread under another SDK
process to bypass ownership. Show edit/pause/resume/clear only where the actual
owner backend supports them. Shared/public views are always read-only.

DSH goal counters represent rounds and activation, not Codex tokens/time. If the
component is reused for DSH, preserve runtime-specific units and revision-based
mutation preconditions. Never convert a round limit into a token budget.

## Per-turn plans

Three data structures require distinct behavior:

- turn/plan/updated replaces the current step list for a turn, with explanation
  and pending/inProgress/completed statuses. Show a small collapsible checklist,
  keyed by turn. Each update replaces prior progress instead of appending a new
  plan to the timeline.
- Native plan items contain Markdown text; item/plan/delta streams text under a
  stable item ID. Reuse the existing Markdown renderer in a labeled plan card,
  with final replacement and reconnect hydration.
- Plan review is a pending interaction. DSH already supplies plan-review intent
  and approveOptionId in notices. Present the full plan alongside explicit
  approval/revision actions through existing interaction plumbing. Never convert
  a display-only plan event into an approval action.

Planning mode is a setting for subsequent model work and remains separate from
both goal state and plan progress. The UI must not imply enabling that mode has
created a plan or started a goal.

## Data flow and validation

Preserve authoritative raw native/IPC state in the connector. Derive a small,
typed presentation container in SessionRuntimeState.metadata; the server already
transports metadata. Clearing requires explicit null/replacement because the
connector state cache merges partial metadata. Keep per-turn plan content in the
timeline rather than storing an unbounded history in session metadata.

The existing web runtimeStatesSemanticallyEqual ignores metadata. Include the
new presentation fields in semantic comparison, or goal/plan-only updates will
be discarded even when updatedSeq advances. Avoid rerendering for unrelated
diagnostic metadata. Test attach, reconnect, clear, budget/status-only changes,
physical turn rollover and read-only rendering.

Command tests cover catalog-to-execution through public adapters, exact raw input,
argument insertion, native errors preserving drafts, unavailable old bridge,
command-specific busy rules and native result correlation. Use headless component
tests and targeted type checks; do not start dev servers or repeatedly build the
application.


## Shared command contract agreed after preflight

Use existing public RuntimeCommand/RuntimeCommandResult metadata rather than new
protocol fields. DSH exact raw input wins even when empty. Without raw, zero args
means `/name`, one string means `/name ` plus that string unchanged, and multiple
args are rejected with a request to provide raw. Non-string args and unsupported
attachments fail before native invocation.

Command descriptors use `metadata.ui`, with this deliberately small discriminator:

```ts
type CommandUi =
  | { kind: 'execute'; argumentHint?: string; acceptsMultiline?: boolean;
      allowedStatuses?: string[] }
  | { kind: 'selector'; target: 'model' | 'reasoning' | 'permission' | 'collaborationMode' };
```

`acceptsArgs` controls editable argument insertion. Keep native DSH input and
optional definitionId separately in metadata; `attachmentsAvailable:false` means
AA has no command attachment transport. DSH descriptors set acceptsMultiline true
because the actual native parser accepts newline input. Other commands must
explicitly support multiline or UI rejects it with the draft intact. DSH native
handlers own busy-state validation; advertise the online session statuses they
accept rather than inheriting a generic idle-only command gate. Codex descriptors
use operation-specific state availability. UI still applies source/read-only/
write-authorization and pending-submit guards. Do not infer required arguments
from hint prose. Do not add the earlier proposed metadata.presentation field.

`session.commands.metadata.catalogRevision` is an opaque string changing across
adapter restart and native command-registry invalidation. Existing platform
runtime.capability.updated (including optional sessionId) carries it. Refresh the
open menu on relevant revision/availability change; reopening and reconnect also
fetch fresh catalogs. Never infer registry invalidation from model output.

Use `result.executionState: 'accepted' | 'completed' | 'unknown'` when the backend
can classify a dispatched operation. Accepted means acknowledged without proof
that background work has finished. Completed requires evidence for that operation.
Generic native DSH success defaults to accepted; preserve its exact native text
and correlation, without claiming all resulting model/background work is done.
Returned native kind:error is ok:false with its known result (completed command
handler, not rollback of effects). Validation/unknown-name rejection need not have
an executionState because nothing was dispatched. Thrown handlers, transport loss
and timeout after dispatch use unknown with retryable:false; no auto-retry and
keep draft. Use this field instead of the earlier proposed result.outcome. UI
must treat ok:false as failure regardless of HTTP status and executionState.

DSH normal execution result keeps commandId, kind, text and sourceEventSeq as
available. Throw/abort does not provide a correlation: never guess from the latest
lifecycle event. Preserve explicit Python cancellation. Shared server Task6b will
forward validated list query/limit, classify ambiguous command timeout, and keep
mutations non-retrying. UI may fetch a full catalog (up to backend limit) on open
and filter locally instead of requesting on each keystroke.


## Concrete presentation and subscription boundaries

The native goal display container is `metadata.codexPresentation` with nullable
`threadGoal` and `completedThreadGoal`. Replace it authoritatively, including null
clears. Plan timeline items use type `artifact`: content kind `plan-progress`
contains explanation and the native plan steps, while content kind `plan` contains
Markdown text. Progress has one stable identity per physical turn; Markdown keeps
its native item identity. Preserve unknown fields/status in native state.

AA background inventory also reads session state. Persistent IPC observation must
therefore begin through an explicit session-view preparation seam, not every
get_session_state call. Add a default-noop runtime hook and binding/RPC forwarding;
Codex attaches read-only and bounds retained follower views. Background snapshots
remain scoped, and missing owners are not claimed by reading. Native operations
and live task ownership retain their separate lifecycle.

Public notice identities are tied to the opaque native response context, including
owner/generation. A fresh request reusing a numeric or string request ID after
reconnect gets a new notice identity, so delayed browser actions cannot target it.
The current native protocol does not expose another owner's SDK generation; record
any residual unobservable remote-restart boundary instead of claiming a guarantee
that the protocol cannot provide.
