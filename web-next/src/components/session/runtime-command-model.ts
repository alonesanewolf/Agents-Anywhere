import type { RuntimeCommand, RuntimeStatusValue, SessionCommandResponse } from "@/features/dashboard/types"
import { isApiError } from "@/lib/api/errors"

export type SlashIntent = { command: string; suffix: string; raw: string; multiline: boolean }

export function parseSlashIntent(raw: string): SlashIntent | null {
  const match = /^\s*\/([^\s]*)([\s\S]*)$/.exec(raw)
  if (!match) return null
  return { command: (match[1] ?? "").toLowerCase(), suffix: match[2] ?? "", raw, multiline: /[\r\n]/.test(raw) }
}

export type CommandUi =
  | { kind: "execute"; argumentHint?: string; acceptsMultiline?: boolean; allowedStatuses?: string[] }
  | { kind: "selector"; target: "model" | "reasoning" | "permission" | "collaborationMode" }

export function commandUi(command: RuntimeCommand): CommandUi | null {
  const value = command.metadata?.ui
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  const ui = value as Record<string, unknown>
  if (ui.kind === "selector" && ["model", "reasoning", "permission", "collaborationMode"].includes(String(ui.target))) {
    return { kind: "selector", target: ui.target as "model" | "reasoning" | "permission" | "collaborationMode" }
  }
  if (ui.kind === "execute") return {
    kind: "execute",
    argumentHint: typeof ui.argumentHint === "string" ? ui.argumentHint : undefined,
    acceptsMultiline: ui.acceptsMultiline === true,
    allowedStatuses: Array.isArray(ui.allowedStatuses) ? ui.allowedStatuses.filter((item): item is string => typeof item === "string") : undefined,
  }
  return null
}

export function exactCommand(intent: SlashIntent, commands: RuntimeCommand[]): RuntimeCommand | null {
  return commands.find(item => item.id.toLowerCase() === intent.command || item.aliases.some(alias => alias.toLowerCase() === intent.command)) ?? null
}

export function commandRequest(intent: SlashIntent | null, command: RuntimeCommand): { command: string; args: string[]; raw: string } | null {
  if (!intent || (intent.multiline && commandUi(command)?.kind === "execute" && !commandUiAllowsMultiline(command))) return null
  if (intent.suffix.trim() && !command.acceptsArgs) return null
  // The raw line is authoritative. String arguments are one free-form value, not
  // an array of words (the public API caps args at 32 entries).
  const args = !intent.suffix.trim() ? [] : command.argsSchema?.type === "string"
    ? [intent.suffix.replace(/^\s/, "")]
    : intent.suffix.trim().split(/\s+/)
  return { command: command.id, args, raw: intent.raw }
}

function commandUiAllowsMultiline(command: RuntimeCommand): boolean {
  const ui = commandUi(command)
  return ui?.kind === "execute" && ui.acceptsMultiline === true
}

export function commandAllowed(command: RuntimeCommand, status: RuntimeStatusValue, capability: boolean, writable: boolean, online: boolean): boolean {
  if (!capability || !writable || !online || !command.enabled) return false
  const ui = commandUi(command)
  if (!ui) return false
  if (ui.kind === "execute") return ui.allowedStatuses?.includes(status) ?? (status === "idle" || status === "error")
  return status === "idle" || status === "error"
}

export function commandActionReason(intent: SlashIntent, command: RuntimeCommand): string | null {
  if (command.id !== "goal") return null
  const action = intent.suffix.trim().split(/\s+/, 1)[0]?.toLowerCase()
  if (!action || action === "status") return null
  const actions = command.metadata.goalActions
  if (!actions || typeof actions !== "object" || Array.isArray(actions)) return null
  const gate = (actions as Record<string, unknown>)[action]
  if (!gate || typeof gate !== "object" || Array.isArray(gate)) return null
  const entry = gate as Record<string, unknown>
  return entry.enabled === false ? (typeof entry.disabledReason === "string" ? entry.disabledReason : "command_unavailable") : null
}

export type CommandOutcome = { ok: boolean; state: "accepted" | "completed" | "unknown"; message: string | null; code: string | null; result: unknown }

export function commandResult(response: Pick<SessionCommandResponse, "ok" | "code" | "message" | "result">): CommandOutcome {
  const data = response.result && typeof response.result === "object" && !Array.isArray(response.result)
    ? response.result as Record<string, unknown> : {}
  const state = data.executionState === "accepted" || data.executionState === "completed" || data.executionState === "unknown"
    ? data.executionState : response.code === "command_outcome_unknown" ? "unknown" : response.ok ? "accepted" : "completed"
  return { ok: response.ok === true && state !== "unknown", state, message: response.message ?? null, code: response.code ?? null, result: response.result }
}

export function commandTransportFailure(error: unknown, fallback: string): CommandOutcome {
  const known = isApiError(error) && error.status >= 400 && error.status < 500
  return {
    ok: false,
    state: known ? "completed" : "unknown",
    code: known ? error.code ?? "command_rejected" : "command_outcome_unknown",
    message: error instanceof Error ? error.message : fallback,
    result: null,
  }
}
