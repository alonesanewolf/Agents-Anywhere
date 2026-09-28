import assert from 'node:assert/strict'
import test from 'node:test'
import { registerSource } from './helpers/onboarding-source.mjs'
const hook = registerSource()
const { sessionSteerResult, sessionSteerFailure } = await import('../src/components/session/session-steer-result.ts')
hook.deregister()
import { ApiError } from '../src/lib/api/errors.ts'

test('outer RPC success is accepted only with native steer evidence', () => {
  assert.deepEqual(sessionSteerResult({ ok: true, result: { steered: true, turnId: 'turn-1' } }), { ok: true, state: 'accepted' })
  assert.deepEqual(sessionSteerResult({ ok: true, result: { ok: false, code: 'codex_no_active_turn', message: 'No active turn' } }), { ok: false, state: 'rejected', message: 'No active turn' })
  assert.deepEqual(sessionSteerResult({ ok: true, result: { steered: false, message: 'No active turn' } }), { ok: false, state: 'rejected', message: 'No active turn' })
  assert.deepEqual(sessionSteerResult({ ok: true, result: {} }), { ok: false, state: 'unknown', message: null })
  assert.deepEqual(sessionSteerResult({ ok: false, result: { steered: true } }), { ok: false, state: 'unknown', message: null })
})

test('explicit HTTP conflict is rejected; transport loss remains unknown', () => {
  assert.deepEqual(sessionSteerFailure(new ApiError({ status: 409, detail: 'Not running', kind: 'http' })), { ok: false, state: 'rejected', message: 'Not running' })
  assert.deepEqual(sessionSteerFailure(new ApiError({ status: 0, detail: 'Connection lost', kind: 'network' })), { ok: false, state: 'unknown', message: 'Connection lost' })
})
