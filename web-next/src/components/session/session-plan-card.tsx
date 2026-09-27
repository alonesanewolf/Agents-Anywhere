"use client"

import * as React from "react"
import { useTranslations } from "next-intl"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { MarkdownText } from "@/components/markdown-text"
import type { SessionView, TimelineItem } from "@/features/dashboard/types"

export function SessionPlanCard({ item, session, token }: { item: TimelineItem; session: SessionView; token: string }) {
  const t = useTranslations("dashboard.session")
  const [open, setOpen] = React.useState(true)
  const progress = item.content.kind === "plan-progress"
  const plan = Array.isArray(item.content.plan) ? item.content.plan : []
  const text = typeof item.content.text === "string" ? item.content.text : ""
  const explanation = typeof item.content.explanation === "string" ? item.content.explanation : null
  return <Collapsible open={open} onOpenChange={setOpen}>
    <Card size="sm" className="gap-2 py-2 shadow-none">
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle className="text-sm">{t(progress ? "planProgressTitle" : "planTextTitle")}</CardTitle>
        <CollapsibleTrigger asChild><Button type="button" variant="ghost" size="xs">{open ? t("goalLess") : t("goalDetails")}</Button></CollapsibleTrigger>
      </CardHeader>
      <CollapsibleContent>
        <CardContent className="min-w-0">
          {progress ? <>
            {explanation ? <p className="mb-2 wrap-break-word text-sm text-muted-foreground">{explanation}</p> : null}
            <ol className="flex flex-col gap-2">
              {plan.map((value, index) => {
                const step = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
                const status = typeof step.status === "string" ? step.status : "unknown"
                const statusKey = `planStep${status.slice(0,1).toUpperCase()}${status.slice(1)}`
                return <li key={index} className="flex min-w-0 items-start gap-2 text-sm">
                  <Badge variant={status === "completed" ? "secondary" : "outline"}>{t.has(statusKey) ? t(statusKey) : status}</Badge>
                  <span className="min-w-0 wrap-break-word">{typeof step.step === "string" ? step.step : t("planUnknownStep")}</span>
                </li>
              })}
            </ol>
          </> : <MarkdownText text={text} token={token} session={session} />}
        </CardContent>
      </CollapsibleContent>
    </Card>
  </Collapsible>
}
