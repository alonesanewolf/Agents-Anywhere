import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { ConnectorLogs, readConnectorLogs, sanitizeConnectorLine } from '../../src/host/connector/logs.js'

test('Connector journal retains 10k lines, pages without gaps during appends, and survives restart', async t => {
  const root = await mkdtemp(join(tmpdir(), 'connector-logs-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const logs = new ConnectorLogs(root)
  logs.output(Array.from({ length: 10_050 }, (_, i) => `line ${i}\n`).join(''))
  await logs.flush()
  const latest = await readConnectorLogs(root)
  assert.equal(latest.entries.length, 200)
  assert.equal(latest.entries.at(-1)?.text, 'line 10049')
  logs.output('new line\n')
  await logs.flush()
  let page = await readConnectorLogs(root, { before: latest.entries[0]!.id })
  const collected = [...page.entries, ...latest.entries]
  while (page.hasMore) {
    page = await readConnectorLogs(root, { before: page.entries[0]!.id })
    collected.unshift(...page.entries)
  }
  assert.equal(collected.length, 9999)
  assert.equal(collected[0]?.text, 'line 51')
  assert.equal(new Set(collected.map(entry => entry.id)).size, collected.length)
  const after = await readConnectorLogs(root, { after: latest.entries.at(-1)!.id })
  assert.deepEqual(after.entries.map(entry => entry.text), ['new line'])
  const restarted = new ConnectorLogs(root)
  restarted.output('after restart\n')
  await restarted.flush()
  assert.ok((await readConnectorLogs(root)).entries.at(-1)!.id > after.entries[0]!.id)
  const persisted = JSON.parse(await readFile(join(root, 'connector-output.json'), 'utf8'))
  assert.equal(persisted.length, 10_000)
  await assert.rejects(readConnectorLogs(root, { before: -1 }))
  await assert.rejects(readConnectorLogs(root, { before: 1, after: 2 }))
})

test('stderr framing preserves split unicode and redacts split credentials before persisting', async t => {
  const root = await mkdtemp(join(tmpdir(), 'connector-redaction-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const logs = new ConnectorLogs(root)
  logs.setSecrets(['private-token'])
  logs.output('\x1b[31m下载 Python: private-')
  logs.output('token\x1b[0m\r\nuv error: connection refused\rfinished')
  logs.finish()
  await logs.flush()
  const result = await readConnectorLogs(root)
  assert.deepEqual(result.entries.map(entry => entry.text), ['下载 Python: [REDACTED]', 'uv error: connection refused', 'finished'])
  assert.doesNotMatch(await readFile(join(root, 'connector-output.json'), 'utf8'), /private-token|\\u001b/)
  assert.doesNotMatch(sanitizeConnectorLine('Authorization: Bearer abc123 https://user:pass@example.test/?token=xyz'), /abc123|user:pass|xyz/)
})


test('starting after factory reset does not republish removed log history', async t => {
  const root = await mkdtemp(join(tmpdir(), 'connector-reset-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const logs = new ConnectorLogs(root)
  logs.output('old session\n')
  await logs.flush()
  await rm(join(root, 'connector-output.json'))
  await logs.startSession([])
  logs.output('new session\n')
  await logs.flush()
  assert.deepEqual((await readConnectorLogs(root)).entries.map(entry => entry.text), ['new session'])
})
