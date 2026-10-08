import assert from 'node:assert/strict'
import test from 'node:test'
import { registerSource } from './helpers/onboarding-source.mjs'
const hooks = registerSource()
const { defaultAdminDateRange, validAdminDateRange, readAdminDateRange } = await import('../src/features/admin/date-range.ts')
hooks.deregister()

test('default range is seven Shanghai calendar days across a UTC date boundary', () => {
  assert.deepEqual(defaultAdminDateRange(new Date('2026-10-07T16:30:00Z')), { from: '2026-10-02', to: '2026-10-08' })
  assert.deepEqual(defaultAdminDateRange(new Date('2026-01-01T00:00:00Z')), { from: '2025-12-26', to: '2026-01-01' })
})
test('invalid calendar dates and inverted ranges are rejected', () => {
  for (const value of [null, {}, { from: '2026-02-30', to: '2026-03-01' }, { from: '2026-10-09', to: '2026-10-08' }, { from: '', to: '2026-10-08' }]) assert.equal(validAdminDateRange(value), false)
  assert.equal(validAdminDateRange({ from: '2024-02-29', to: '2024-02-29' }), true)
})
test('persisted ranges survive page remounts and inaccessible storage falls back', (t) => {
  const range = { from: '2026-09-01', to: '2026-09-30' }
  const original = globalThis.window
  globalThis.window = { sessionStorage: { getItem: key => key === 'user1' ? JSON.stringify(range) : '{invalid' } }
  t.after(() => { if (original === undefined) delete globalThis.window; else globalThis.window = original })
  assert.deepEqual(readAdminDateRange('user1'), range)
  assert.equal(validAdminDateRange(readAdminDateRange('user2')), true)
  window.sessionStorage.getItem = () => { throw new Error('blocked') }
  assert.equal(validAdminDateRange(readAdminDateRange('user1')), true)
})
