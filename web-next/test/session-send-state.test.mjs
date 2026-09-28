import assert from 'node:assert/strict'
import test from 'node:test'
import { createDetailStateHarness, runtimeEvent } from './helpers/session-detail-state.mjs'

function timelineSnapshot(sequence) {
  return { protocolVersion: '1.0', eventId: `timeline:${sequence}`, sessionId: 's1', type: 'timeline.snapshot', sequence, cursor: `seq:${sequence}`, payload: { items: [] } }
}

test('history-first Codex view refuses input until live synchronization completes', async t => {
  let sends = 0
  let steers = 0
  const harness = createDetailStateHarness({
    send: async () => { sends++; return { ok: true } },
    steer: async () => { steers++; return { ok: true, result: { steered: true } } },
  })
  t.after(() => harness.dispose())
  harness.replaceRenderState(current => ({ ...current, runtimeSyncPending: true }))
  assert.equal(await harness.handleSend('wait for the owner', [], {}), false)
  harness.applyEvent(runtimeEvent('running', 121))
  harness.renderBuffer.flush()
  assert.equal((await harness.handleSteer('not yet', [])).ok, false)
  assert.equal(sends, 0)
  assert.equal(steers, 0)
  harness.replaceRenderState(current => ({ ...current, runtimeSyncPending: false }))
  assert.equal((await harness.handleSteer('ready', [])).state, 'accepted')
  assert.equal(steers, 1)
})

test('a recovered native projection retries live synchronization while input stays locked', async t => {
  const recoveries = []
  const harness = createDetailStateHarness({ onLiveRecovery: (reason, connection) => { recoveries.push([reason, connection]) } })
  t.after(() => harness.dispose())
  harness.environment.needsLiveSnapshot = true
  harness.replaceRenderState(current => ({ ...current, runtimeSyncPending: true }))
  harness.applyEvent(runtimeEvent('idle', 121, { metadata: { codexCoordination: { role: 'unattached', available: false } } }))
  assert.deepEqual(recoveries, [])
  harness.applyEvent(runtimeEvent('idle', 122, { metadata: { codexCoordination: { role: 'unattached', available: true } } }))
  assert.deepEqual(recoveries, [['runtime-state-recovered', 1]])
  assert.equal(harness.state.runtimeSyncPending, true)
})

test('ordinary start reconciles published timeline 123 then native running 122 without reload', async t => {
  let finish
  let sends = 0
  let steers = 0
  const harness = createDetailStateHarness({ send: () => { sends++; return new Promise(resolve => { finish = resolve }) }, steer: async () => { steers++; return { ok: true, result: { steered: true } } } })
  t.after(() => harness.dispose())
  const pending = harness.handleSend('start task', [], {})
  assert.equal(harness.state.state.status, 'waiting')
  harness.applyEvent(runtimeEvent('waiting', 121))
  harness.applyEvent(timelineSnapshot(123))
  harness.applyEvent(runtimeEvent('running', 122))
  harness.renderBuffer.flush()
  assert.equal(harness.state.state.status, 'running')
  assert.equal(harness.state.state.metadata.codexLatestTurn.id, 'turn-current')
  assert.equal(harness.state.nextSeq, 123)
  assert.equal(harness.state.eventCursor, 'seq:123')
  assert.equal(await harness.handleSend('duplicate', [], {}), false)
  assert.equal((await harness.handleSteer('pending steer', [])).ok, false)
  assert.equal(sends, 1)
  assert.equal(steers, 0)
  finish({ ok: true })
  assert.equal(await pending, true)
  assert.equal(harness.sending, false)
  assert.equal((await harness.handleSteer('steer current task', [])).state, 'accepted')
  assert.equal(steers, 1)
  assert.equal(sends, 1)
})

test('settled ordinary POST does not fabricate running before its authoritative projection', async t => {
  const harness = createDetailStateHarness()
  t.after(() => harness.dispose())
  assert.equal(await harness.handleSend('start task', [], {}), true)
  assert.equal(harness.state.state.status, 'waiting')
  assert.equal((await harness.handleSteer('too early', [])).ok, false)
  harness.applyEvent(timelineSnapshot(130))
  harness.renderBuffer.flush()
  harness.applyEvent(runtimeEvent('running', 122))
  harness.renderBuffer.flush()
  assert.equal(harness.state.state.status, 'running')
  assert.equal((await harness.handleSteer('now', [])).state, 'accepted')
})

test('buffered runtime projection survives a newer render-state timeline cursor', t => {
  const harness = createDetailStateHarness()
  t.after(() => harness.dispose())
  harness.applyEvent(runtimeEvent('running', 122))
  harness.replaceRenderState(current => ({ ...current, nextSeq: 130, eventCursor: 'seq:130' }))
  harness.renderBuffer.flush()
  assert.equal(harness.state.state.status, 'running')
  assert.equal(harness.state.nextSeq, 130)
  assert.equal(harness.state.eventCursor, 'seq:130')
})

test('same-semantic runtime observations advance their watermark before older contradictory state', t => {
  const harness = createDetailStateHarness()
  t.after(() => harness.dispose())
  harness.applyEvent(runtimeEvent('running', 122))
  harness.renderBuffer.flush()
  harness.applyEvent(runtimeEvent('running', 128))
  harness.renderBuffer.flush()
  assert.equal(harness.state.state.updatedSeq, 128)
  harness.applyEvent(runtimeEvent('idle', 125))
  harness.renderBuffer.flush()
  assert.equal(harness.state.state.status, 'running')
  harness.applyEvent(runtimeEvent('idle', 129))
  harness.renderBuffer.flush()
  assert.equal(harness.state.state.status, 'idle')
})

test('runtime admission keeps identity, durable ordering, same-sequence transitions and cancellation', t => {
  const harness = createDetailStateHarness()
  t.after(() => harness.dispose())
  harness.applyEvent(timelineSnapshot(130))
  harness.applyEvent(runtimeEvent('running', 122))
  harness.applyEvent(runtimeEvent('idle', 122))
  harness.applyEvent(runtimeEvent('running', 122))
  harness.renderBuffer.flush()
  assert.equal(harness.state.state.status, 'running')
  const resetVersion = harness.state.timelineResetVersion
  harness.applyEvent(timelineSnapshot(125))
  harness.applyEvent({ ...runtimeEvent('idle', 140), sessionId: 'other' })
  harness.applyEvent(runtimeEvent('idle', 140, { sessionId: 'other' }))
  harness.renderBuffer.flush()
  assert.equal(harness.state.state.status, 'running')
  assert.equal(harness.state.timelineResetVersion, resetVersion)
  harness.dispose()
  harness.applyEvent(runtimeEvent('idle', 150))
  assert.equal(harness.state.state.status, 'running')
})
