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
  const known = isApiError(error) && error.status >= 400 && error.status < 500
  return { ok: false, state: known ? "rejected" : "unknown", message: error instanceof Error ? error.message : null }
}
