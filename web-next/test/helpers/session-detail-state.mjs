import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { registerSource } from './onboarding-source.mjs'

const hook = registerSource()
const modules = await Promise.all([
  import('../../src/components/session/session-event-state.ts'),
  import('../../src/components/session/session-event-buffer.ts'),
  import('../../src/components/session/runtime-presentation.ts'),
  import('../../src/components/session/optimistic-timeline.ts'),
  import('../../src/components/session/session-review-history.ts'),
  import('../../src/components/session/session-steer-result.ts'),
  import('../../src/components/session/capabilities.ts'),
  import('../../src/lib/id.ts'),
])
hook.deregister()
const dependencies = Object.assign({}, ...modules)
const source = readFileSync(new URL('../../src/components/session-detail.tsx', import.meta.url), 'utf8')
const ast = ts.createSourceFile('session-detail.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)

// Execute the current private production closures, including BOTH event gates,
// without exporting test-only APIs or mocking the event/reducer implementation.
function declaration(name) {
  let found
  function visit(node) {
    if ((ts.isFunctionDeclaration(node) || ts.isVariableDeclaration(node)) && node.name?.getText(ast) === name) found = node
    ts.forEachChild(node, visit)
  }
  visit(ast)
  assert.ok(found, `production declaration ${name}`)
  return ts.isVariableDeclaration(found) ? `const ${found.getText(ast)};` : found.getText(ast)
}
const declarations = [
  'mergeSessionEvent', 'sessionEventCanUpdateState', 'readPayloadValue', 'isTimelineItem', 'isNotice',
  'catalogUpdateFromEvent', 'catalogsSemanticallyEqual', 'sessionSemanticallyEqual', 'mergeNotices',
  'nextOptimisticRuntimeState', 'selectionPatchFromComposerSelections', 'runtimeStateWithSelectionResult',
  'runtimeStateWithSelections', 'renderBuffer', 'applyEvent', 'handleSend', 'handleSteer',
]
const code = ts.transpileModule(declarations.map(declaration).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
// `with` supplies the surrounding component/effect bindings to unchanged
// closures. Rendering and HTTP are the boundaries; all state logic is real.
const create = new Function('environment', `with (environment) { ${code}\nreturn { applyEvent, renderBuffer, handleSend, handleSteer }; }`)

export function runtimeEvent(status, sequence, overrides = {}) {
  return {
    protocolVersion: '1.0', eventId: `runtime:${sequence}:${status}`, type: 'runtime.state.updated',
    sessionId: 's1', sequence, cursor: `seq:${sequence}`, emittedAt: '2026-09-28T00:00:00Z',
    payload: { state: { sessionId: 's1', runtime: 'codex', runtimeId: 'codex', externalSessionId: 'thread-1',
      status, updatedSeq: sequence, selections: {}, metadata: { codexLatestTurn: { id: 'turn-current', status: status === 'running' ? 'inProgress' : 'completed' } },
      ...overrides } },
  }
}

export function createDetailStateHarness({ onChange = () => {}, send, steer, onLiveRecovery = () => {} } = {}) {
  let state = {
    session: { id: 's1', runtime: 'codex', runtimeId: 'codex', externalSessionId: 'thread-1', connectorStatus: 'online', status: 'idle', updatedSeq: 120, takeover: true, archived: false },
    state: runtimeEvent('idle', 120).payload.state, items: [], notices: [], catalogs: {}, timelineResetVersion: 0,
    nextSeq: 120, eventCursor: 'seq:120', effectiveCapabilities: { revision: 1, capabilities: ['session.send_message', 'session.steer', 'session.interrupt'].map(capabilityId => ({
      capabilityId, scope: 'session', runtime: 'codex', runtimeId: 'codex', sessionId: 's1', supported: true, available: true, allowed: true,
    })) },
  }
  let sending = false
  const environment = {
    ...dependencies, console: { info() {} }, token: 'test', sessionId: 's1', cancelled: false,
    needsLiveSnapshot: false, socketSubscribed: true, liveSnapshotLoading: false, liveRetryScheduled: false,
    recoveryPromise: null, recoveryStarting: false, connectionSequence: 1,
    recoverAfterSubscription: onLiveRecovery,
    get state() { return state }, get session() { return state.session }, get runtimeState() { return state.state },
    get runtimeStatus() { return state.state.status }, get sending() { return sending },
    sessionRuntimeId: session => session.runtimeId, sessionRuntimeType: session => session.runtime,
    setState(update) { state = update(state); onChange() }, setSending(value) { sending = value; onChange() },
    eventSequenceCursor: new dependencies.SessionEventSequenceCursor('s1', 120), processedEventIds: new Set(),
    clearResolvedOptimisticMessagesRef: { current() {} }, recoverEvents() { assert.fail('unexpected event recovery') },
    sessionVisitRef: { current: { sessionId: 's1' } }, activeSendRequestRef: { current: null }, activeSteerRequestRef: { current: null },
    sendRequestSeqRef: { current: 0 }, steerRequestSeqRef: { current: 0 }, timelineFollowRef: { current: null }, selectionWritesRef: { current: new Map() },
    addOptimisticMessage() {}, tNew: key => key, tSession: key => key, interrupting: false, blockingInteractionCount: 0,
    dashboardApi: {
      sendSessionMessage: send ?? (async () => ({ ok: true })),
      steerSession: steer ?? (async () => ({ ok: true, result: { steered: true } })),
      updateSessionSelections() { assert.fail('unexpected settings write') },
    },
  }
  const production = create(environment)
  return {
    ...production,
    get state() { return state }, get sending() { return sending }, environment,
    replaceRenderState(update) { state = update(state) },
    dispose() { environment.cancelled = true; production.renderBuffer.dispose() },
  }
}
