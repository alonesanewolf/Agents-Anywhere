import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { RuntimeServer } from '../../src/host/dsh-runtime/server.js'

const reader = { query: { listSessions: async () => [], readTitleSnapshots: async () => [], readSession: async () => { throw new Error('unused') } }, status: () => undefined }
for (const descriptor of [JSON.stringify({ pid: process.pid, token: 'stale-live-host' }), '{truncated', 'null']) {
  test(`exclusive owner replaces stale descriptor ${descriptor}`, async t => {
    const home = await mkdtemp(join(tmpdir(), 'bridge-stale-'))
    const file = join(home, 'bridge', 'endpoint.json')
    await mkdir(join(home, 'bridge'))
    await writeFile(file, descriptor)
    const owner = new RuntimeServer(file, reader)
    const contender = new RuntimeServer(file, reader)
    t.after(async () => { await Promise.all([owner.close(), contender.close()]); await rm(home, { recursive: true, force: true }) })
    const endpoint = await owner.start()
    assert.equal(JSON.parse(await readFile(file, 'utf8')).token, endpoint.token)
    await assert.rejects(contender.start(), /另一个插件实例/)
    assert.equal(JSON.parse(await readFile(file, 'utf8')).token, endpoint.token)
    await owner.close()
    assert.ok((await contender.start()).token !== endpoint.token)
  })
}

test('crashed bridge releases lease and a fresh instance reclaims its descriptor', { timeout: 15_000 }, async t => {
  const home = await mkdtemp(join(tmpdir(), 'bridge-crash-'))
  const file = join(home, 'bridge', 'endpoint.json')
  const module = new URL('../../src/host/dsh-runtime/server.ts', import.meta.url).href
  const script = `import { RuntimeServer } from ${JSON.stringify(module)}; const server = new RuntimeServer(process.argv[1], { query: { listSessions: async () => [], readTitleSnapshots: async () => [], readSession: async () => { throw new Error('unused') } }, status: () => undefined }); await server.start(); process.send('ready');`
  const child = spawn(process.execPath, ['--import', 'tsx', '--input-type=module', '--eval', script, file], { stdio: ['ignore', 'ignore', 'pipe', 'ipc'] })
  const owner = new RuntimeServer(file, reader)
  const ended = once(child, 'exit')
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); await ended; await owner.close(); await rm(home, { recursive: true, force: true }) })
  await Promise.race([once(child, 'message'), ended.then(() => { throw new Error('bridge child failed before readiness') })])
  const stale = JSON.parse(await readFile(file, 'utf8'))
  child.kill('SIGKILL')
  await ended
  const endpoint = await owner.start()
  assert.notEqual(endpoint.token, stale.token)
})
