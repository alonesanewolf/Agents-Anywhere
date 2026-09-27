"use client"

import * as React from "react"
import { dashboardApi } from "@/features/dashboard/api"
import type { RuntimeCommand } from "@/features/dashboard/types"

export function useRuntimeCommands({ token, sessionId, open, available, catalogRevision }: {
  token: string
  sessionId: string | null
  open: boolean
  available: boolean
  catalogRevision: string
}): {commands: RuntimeCommand[]; loading: boolean; error: boolean} {
  const [commands, setCommands] = React.useState<RuntimeCommand[]>([])
  const [loading, setLoading] = React.useState(false)
  const [error, setError] = React.useState(false)
  React.useEffect(() => {
    if (!open || !sessionId || !available) {
      setCommands([])
      setLoading(false)
      setError(false)
      return
    }
    let cancelled = false
    setCommands([])
    setLoading(true)
    setError(false)
    const timer = window.setTimeout(() => {
      void dashboardApi.getSessionCommands(token, sessionId).then(response => {
        if (!cancelled) setCommands(response.commands)
      }).catch(() => {
        if (!cancelled) { setCommands([]); setError(true) }
      }).finally(() => { if (!cancelled) setLoading(false) })
    }, 120)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [token, sessionId, open, available, catalogRevision])
  return {commands,loading,error}
}
