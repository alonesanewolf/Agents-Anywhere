export type AdminDateRange = { from: string; to: string }

export function defaultAdminDateRange(now = new Date()): AdminDateRange {
  const to = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now)
  const start = new Date(`${to}T00:00:00Z`)
  start.setUTCDate(start.getUTCDate() - 6)
  return { from: start.toISOString().slice(0, 10), to }
}

export function validAdminDateRange(value: unknown): value is AdminDateRange {
  if (!value || typeof value !== "object") return false
  const { from, to } = value as AdminDateRange
  const validDate = (date: unknown): date is string => {
    if (typeof date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return false
    const parsed = new Date(`${date}T00:00:00Z`)
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date
  }
  return validDate(from) && validDate(to) && from <= to
}

export function readAdminDateRange(key: string): AdminDateRange {
  try {
    const stored: unknown = JSON.parse(window.sessionStorage.getItem(key) ?? "null")
    if (validAdminDateRange(stored)) return stored
  } catch {
    /* Storage may be unavailable. */
  }
  return defaultAdminDateRange()
}
