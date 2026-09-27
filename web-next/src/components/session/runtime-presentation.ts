import type { SessionRuntimeState, TimelineItem } from "@/features/dashboard/types"
import { stableStringify } from "@/components/session/session-event-state"

export type NativeGoal = Record<string, unknown> & {
  objective: string
  status: string
  tokensUsed?: number
  timeUsedSeconds?: number
  tokenBudget?: number | null
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
}

export function goalPresentation(state: SessionRuntimeState | null | undefined): Record<string, unknown> | null {
  return record(state?.metadata?.codexPresentation)
}

export function readGoal(state: SessionRuntimeState | null | undefined): NativeGoal | null {
  const presentation = goalPresentation(state)
  const current = record(presentation?.threadGoal)
  const completed = record(presentation?.completedThreadGoal)
  const selected = current ?? completed
  return selected && typeof selected.objective === "string" && typeof selected.status === "string" ? selected as NativeGoal : null
}

export function presentationSemantics(state: SessionRuntimeState | null | undefined): Record<string, unknown> {
  const metadata = state?.metadata ?? {}
  const display = goalPresentation(state)
  const values: Record<string, unknown> = {}
  if (display) {
    values.codexPresentation = {
      ...("threadGoal" in display ? { threadGoal: display.threadGoal } : {}),
      ...("completedThreadGoal" in display ? { completedThreadGoal: display.completedThreadGoal } : {}),
    }
  }
  // Availability/role and mode changes affect controls even without a status
  // change. Ignore diagnostic stream revisions and unrelated metadata.
  for (const field of ["codexCoordination", "codexCapabilities", "codexSettings"]) {
    const source = record(metadata[field])
    if (!source) continue
    if (field === "codexCoordination") values[field] = { role: source.role, available: source.available, generation: source.generation }
    if (field === "codexCapabilities") values[field] = { goalControl: source.goalControl, userSessionStop: source.userSessionStop }
    if (field === "codexSettings") values[field] = {
      collaborationMode: record(record(source.latestThreadSettings)?.collaborationMode)?.mode,
    }
  }
  return values
}

export function runtimeStatesSemanticallyEqual(left: SessionRuntimeState | null, right: SessionRuntimeState): boolean {
  if (!left) return false
  const semantic = (value: SessionRuntimeState) => ({
    sessionId:value.sessionId, runtime:value.runtime, externalSessionId:value.externalSessionId,
    status:value.status, selections:value.selections, statusReason:value.statusReason,
    error:value.error, presentation:presentationSemantics(value),
  })
  return stableStringify(semantic(left)) === stableStringify(semantic(right))
}

export function latestPlanItems<T extends Pick<TimelineItem, "id" | "type" | "content" | "source" | "orderSeq" | "updatedSeq">>(items: readonly T[]): T[] {
  const latest = new Map<string, T>()
  for (const item of items) {
    if (item.type !== "artifact" || item.content.kind !== "plan-progress") continue
    const turnId = typeof item.source.turnId === "string" ? item.source.turnId : item.id
    const previous = latest.get(turnId)
    if (!previous || item.updatedSeq > previous.updatedSeq || (item.updatedSeq === previous.updatedSeq && item.orderSeq >= previous.orderSeq)) latest.set(turnId, item)
  }
  return items.filter(item => item.type !== "artifact" || item.content.kind !== "plan-progress" || latest.get(typeof item.source.turnId === "string" ? item.source.turnId : item.id) === item)
}
