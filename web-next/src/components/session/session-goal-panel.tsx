"use client"

import * as React from "react"
import { useTranslations } from "next-intl"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Button } from "@/components/ui/button"
import type { SessionRuntimeState } from "@/features/dashboard/types"
import { readGoal } from "@/components/session/runtime-presentation"

export function SessionGoalPanel({ state }: { state: SessionRuntimeState | null | undefined }) {
  const t = useTranslations("dashboard.session")
  const goal = readGoal(state)
  const [open, setOpen] = React.useState(false)
  if (!goal) return null
  const completed = goal.status === "complete"
  const statusKey = `goalStatus${goal.status.slice(0, 1).toUpperCase()}${goal.status.slice(1)}`
  const status = t.has(statusKey) ? t(statusKey) : goal.status
  const usage = [
    Number.isFinite(goal.tokensUsed) ? t("goalTokens", { count: goal.tokensUsed! }) : null,
    Number.isFinite(goal.timeUsedSeconds) ? t("goalTime", { count: goal.timeUsedSeconds! }) : null,
    Number.isFinite(goal.tokenBudget) ? t("goalBudget", { count: goal.tokenBudget! }) : null,
  ].filter(Boolean).join(" · ")
  const content = (
    <Card size="sm" className="gap-2 py-2 shadow-none">
      <CardHeader className="flex flex-row items-center gap-2">
        <CardTitle className="min-w-0 flex-1 text-sm">{t("goalTitle")}</CardTitle>
        <Badge variant={completed ? "secondary" : "outline"}>{status}</Badge>
        {completed ? <CollapsibleTrigger asChild><Button variant="ghost" size="xs">{open ? t("goalLess") : t("goalDetails")}</Button></CollapsibleTrigger> : null}
      </CardHeader>
      <CardContent className="min-w-0">
        <p className="wrap-break-word text-sm">{goal.objective}</p>
        {usage && !completed ? <CardDescription className="mt-1 text-xs">{usage}</CardDescription> : null}
        {completed ? <CollapsibleContent><CardDescription className="mt-1 text-xs">{usage}</CardDescription></CollapsibleContent> : null}
      </CardContent>
    </Card>
  )
  return <div className="mx-auto w-full max-w-3xl px-4 pb-1">{completed ? <Collapsible open={open} onOpenChange={setOpen}>{content}</Collapsible> : content}</div>
}
