export interface ConnectorLogEntry { id: number; time: string; text: string }
export interface ConnectorLogQuery { before?: number; after?: number }
export interface ConnectorLogPage {
  entries: ConnectorLogEntry[]
  hasMore: boolean
  oldestId: number | null
  newestId: number | null
}

export interface BridgeLogEntry {
  id?: string
  time: string
  level: 'debug' | 'info' | 'warn' | 'error'
  event: string
  details: string
  method?: string
  outcome?: 'success' | 'failure' | 'pending' | 'info'
}

export interface BridgeLogSnapshot {
  entries: BridgeLogEntry[]
  updatedAt: string
}
