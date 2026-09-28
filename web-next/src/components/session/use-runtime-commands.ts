"use client"

import * as React from "react"
import { dashboardApi } from "@/features/dashboard/api"
import type { RuntimeCommand } from "@/features/dashboard/types"

export function createRecoveredSubscriptionTracker(onReconnectRecovered: () => void) {
  let initialConnection: number | null = null
  let lastRecoveredConnection: number | null = null
  return {
    observed(connection: number) {
      if (initialConnection === null) initialConnection = connection
    },
    recovered(connection: number) {
      if (lastRecoveredConnection === connection) return
      lastRecoveredConnection = connection
      if (initialConnection !== null && connection !== initialConnection) onReconnectRecovered()
    },
  }
}

export function useRuntimeCommands({ token, sessionId, open, available, catalogRevision, recoveryGeneration = 0 }: {
  token: string
  sessionId: string | null
  open: boolean
  available: boolean
  catalogRevision: string
  recoveryGeneration?: number
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
  }, [token, sessionId, open, available, catalogRevision, recoveryGeneration])
  return {commands,loading,error}
}
