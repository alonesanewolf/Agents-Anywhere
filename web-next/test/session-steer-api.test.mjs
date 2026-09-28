import assert from 'node:assert/strict'
import test from 'node:test'
import { registerSource } from './helpers/onboarding-source.mjs'
const hook = registerSource()
const { DashboardApi } = await import('../src/features/dashboard/api.ts')
hook.deregister()

test('steer builds the encoded session route and exact raw body with uploaded references', async () => {
  const requests = []
  const api = new DashboardApi({ post: async (path, body, options) => {
    requests.push({ path, body, options })
    return { ok: true, result: { steered: true, turnId: 'turn-1' } }
  } })
  await api.steerSession('token', 's /1', '  follow this\nnow  ', {
    attachments: [{ fileId: 'file-1' }], clientMessageId: 'msg-one',
  })
  assert.deepEqual(requests, [{
    path: '/sessions/s%20%2F1/runtime/steer',
    body: { content: '  follow this\nnow  ', attachments: [{ fileId: 'file-1' }], clientMessageId: 'msg-one' },
    options: { token: 'token' },
  }])
})
