import type { RpcResponse } from "@/features/dashboard/types"

export function sessionStopOutcome(
  response: RpcResponse<unknown>,
  message: (key: "stopUnknownOutcome" | "stopPartialTurn" | "stopAccepted", values?: { turn: string }) => string,
): { ok: boolean; message: string } {
  const payload = response.result && typeof response.result === "object" && !Array.isArray(response.result)
    ? response.result as Record<string, unknown> : {}
  const details = payload.result && typeof payload.result === "object" && !Array.isArray(payload.result)
    ? payload.result as Record<string, unknown> : payload
  const unknown = response.ok !== true || payload.executionState === "unknown" || details.executionState === "unknown"
  if (unknown) {
    const interruptedTurn = typeof details.interruptedTurnId === "string" && details.interruptedTurnId ? details.interruptedTurnId : null
    return { ok: false, message: `${message("stopUnknownOutcome")}${interruptedTurn ? ` ${message("stopPartialTurn", { turn: interruptedTurn })}` : ""}` }
  }
  return {ok:true,message:message("stopAccepted")}
}
