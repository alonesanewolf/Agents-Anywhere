import assert from 'node:assert/strict'
import test from 'node:test'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { resolveUv } from '../../src/host/connector/environment.js'
import { DEFAULT_CONNECTOR_SETTINGS } from '../../src/contracts/connector.js'

const config = { stateRoot: '/unused', connectorSourceDir: '/unused', uvPath: 'uv', apiBaseUrl: 'https://example.test' }
test('npm uv resolves its native optional dependency with install scripts disabled', async () => {
  const binary = await resolveUv(config, DEFAULT_CONNECTOR_SETTINGS)
  assert.ok(binary)
  assert.match(binary.replaceAll('\\', '/'), /@dataiku\/uv-[^/]+\/bin\/uv(?:\.exe)?$/)
  const { stdout } = await promisify(execFile)(binary, ['--version'], { timeout: 10_000 })
  assert.match(stdout, /^uv 0\.12\.0\b/)
})
test('explicit uv overrides take priority over the npm dependency', async () => {
  assert.equal(await resolveUv(config, { ...DEFAULT_CONNECTOR_SETTINGS, uvPath: process.execPath }), process.execPath)
  assert.equal(await resolveUv({ ...config, uvPath: process.execPath }, DEFAULT_CONNECTOR_SETTINGS), process.execPath)
})
