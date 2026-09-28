import { join } from 'node:path'
import { stripVTControlCharacters } from 'node:util'
import { readJson, writeJson } from '../storage/files.js'
import type { ConnectorLogEntry, ConnectorLogPage, ConnectorLogQuery } from '../../contracts/logs.js'

export const MAX_CONNECTOR_LINES = 10_000
export const CONNECTOR_PAGE_SIZE = 200
const MAX_LINE = 4096
const filename = 'connector-output.json'

export function sanitizeConnectorLine(text: string, secrets: readonly string[] = []): string {
  let safe = stripVTControlCharacters(text).replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '')
  for (const secret of secrets.filter(Boolean).sort((a, b) => b.length - a.length)) {
    safe = safe.split(secret).join('[REDACTED]').split(encodeURIComponent(secret)).join('[REDACTED]')
  }
  return safe.replace(/(\b(?:Bearer|Basic)\s+)[^\s,"'}]+/gi, '$1[REDACTED]')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1[REDACTED]@')
    .replace(/([?&](?:token|access_token|authToken|connectorToken|key|api_key|password|secret)=)[^\s&#]+/gi, '$1[REDACTED]')
    .replace(/((?:authorization|bearer|password|secret|(?:access[_-]?|connector|auth|refresh[_-]?)?token|api[_-]?key)["']?\s*[:=]?\s*["']?)[^\s,"'}]+/gi, '$1[REDACTED]')
}

async function entriesAt(directory: string): Promise<ConnectorLogEntry[]> {
  const saved = await readJson<ConnectorLogEntry[]>(join(directory, filename))
  if (!Array.isArray(saved)) return []
  return saved.filter(entry => Number.isSafeInteger(entry?.id) && typeof entry.time === 'string' && typeof entry.text === 'string').slice(-MAX_CONNECTOR_LINES)
}

export async function readConnectorLogs(directory: string, query: ConnectorLogQuery = {}): Promise<ConnectorLogPage> {
  if (!query || typeof query !== 'object' || Array.isArray(query) ||
    Object.keys(query).some(key => key !== 'before' && key !== 'after') ||
    [query.before, query.after].some(value => value !== undefined && (!Number.isSafeInteger(value) || value < 0)) ||
    (query.before !== undefined && query.after !== undefined)) throw new Error('Connector 日志分页参数无效。')
  const entries = await entriesAt(directory)
  const candidates = entries.filter(entry => (query.before === undefined || entry.id < query.before) && (query.after === undefined || entry.id > query.after))
  return {
    entries: query.after === undefined ? candidates.slice(-CONNECTOR_PAGE_SIZE) : candidates.slice(0, CONNECTOR_PAGE_SIZE),
    hasMore: candidates.length > CONNECTOR_PAGE_SIZE,
    oldestId: entries[0]?.id ?? null, newestId: entries.at(-1)?.id ?? null,
  }
}

/** One writer per manager lease. Atomic snapshots retain at most 10k terminal lines across restarts. */
export class ConnectorLogs {
  private entries: ConnectorLogEntry[] = []
  private ready: Promise<void>
  private writes: Promise<void> = Promise.resolve()
  private timer: ReturnType<typeof setTimeout> | undefined
  private remainder = ''
  private secrets: string[] = []
  private sequence = 0
  private dirty = false
  constructor(private readonly directory: string) {
    this.ready = entriesAt(directory).catch(() => []).then(entries => {
      this.entries = entries
      this.sequence = Math.max(Date.now() * 1000, entries.at(-1)?.id ?? 0)
    })
  }
  setSecrets(secrets: string[]): void { this.secrets = secrets.filter(Boolean) }
  async startSession(secrets: string[]): Promise<void> {
    await this.flush()
    // Factory reset can remove the journal while this manager remains mounted.
    this.entries = await entriesAt(this.directory).catch(() => [])
    this.sequence = Math.max(Date.now() * 1000, this.entries.at(-1)?.id ?? 0)
    this.setSecrets(secrets)
  }
  record(event: string): void { this.append(`[process] ${event}`) }
  output(chunk: string): void {
    this.remainder += chunk
    const lines = this.remainder.split(/\r\n|[\r\n]/)
    this.remainder = lines.pop() ?? ''
    for (const line of lines) if (line) this.append(line)
    // Do not split a possible credential across persisted lines.
    if (this.remainder.length > MAX_LINE * 4) this.remainder = '[overlong output omitted]'
  }
  finish(): void {
    if (this.remainder) this.append(this.remainder)
    this.remainder = ''
  }
  private append(text: string): void {
    const time = new Date().toISOString()
    const safe = sanitizeConnectorLine(text, this.secrets)
    const line = safe.length > MAX_LINE ? safe.slice(0, MAX_LINE) + '…' : safe
    this.ready = this.ready.then(() => {
      this.entries.push({ id: ++this.sequence, time, text: line })
      if (this.entries.length > MAX_CONNECTOR_LINES) this.entries.splice(0, this.entries.length - MAX_CONNECTOR_LINES)
      this.dirty = true
    })
    if (!this.timer) {
      this.timer = setTimeout(() => { this.timer = undefined; void this.flush().catch(() => undefined) }, 100)
      this.timer.unref()
    }
  }
  async flush(): Promise<void> {
    if (this.timer) { clearTimeout(this.timer); this.timer = undefined }
    await this.ready
    if (this.dirty) {
      this.writes = this.writes.catch(() => undefined).then(async () => {
        await this.ready
        if (!this.dirty) return
        this.dirty = false
        try { await writeJson(join(this.directory, filename), [...this.entries]) }
        catch (error) { this.dirty = true; throw error }
      })
    }
    await this.writes
  }
}
