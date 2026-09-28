import type { RpcResponse, SessionSteerResult } from "@/features/dashboard/types"
import { isApiError } from "@/lib/api/errors"

export type SteerOutcome = { ok: boolean; state: "accepted" | "rejected" | "unknown"; message?: string | null }

export function sessionSteerResult(response: RpcResponse<SessionSteerResult>): SteerOutcome {
  const result = response.result
  if (response.ok === true && result?.steered === true && result.ok !== false && result.executionState !== "unknown") {
    return { ok: true, state: "accepted" }
  }
  if (response.ok === true && (result?.ok === false || result?.steered === false)) {
    return { ok: false, state: "rejected", message: result.message ?? null }
  }
  return { ok: false, state: "unknown", message: null }
}

export function sessionSteerFailure(error: unknown): SteerOutcome {
  const known = isApiError(error) &&
    ([400, 401, 403, 404, 405, 413, 415, 422].includes(error.status) ||
      (error.status === 409 && isKnownSteerPreflightConflict(error.detail)))
  return { ok: false, state: known ? "rejected" : "unknown", message: error instanceof Error ? error.message : null }
}

function isKnownSteerPreflightConflict(detail: string): boolean {
  return detail === "session is not running" || detail === "connector is offline" ||
    detail === "session is read-only until takeover is enabled"
}
