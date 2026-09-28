"use client"

import * as React from "react"
import { useTranslations } from "next-intl"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Button } from "@/components/ui/button"
import { MarkdownText } from "@/components/markdown-text"
import type { SessionView, TimelineItem } from "@/features/dashboard/types"

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
}

export function SessionInputResponseCard({ item, session }: { item: TimelineItem; session: SessionView }) {
  const t = useTranslations("dashboard.session")
  const [open, setOpen] = React.useState(true)
  const questions = Array.isArray(item.content.questions) ? item.content.questions : []
  const answers = record(item.content.answers)
  const state = item.content.state === "awaiting" || item.content.state === "answered" ? item.content.state : "unknown"
  return <Collapsible open={open} onOpenChange={setOpen}>
    <Card size="sm" className="gap-2 py-2 shadow-none">
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle className="text-sm">{t("inputResponseTitle")} · {t(state === "answered" ? "inputResponseAnswered" : state === "awaiting" ? "inputResponseAwaiting" : "inputResponseUnknown")}</CardTitle>
        <CollapsibleTrigger asChild><Button type="button" variant="ghost" size="xs">{t(open ? "goalLess" : "goalDetails")}</Button></CollapsibleTrigger>
      </CardHeader>
      <CollapsibleContent>
        <CardContent className="flex min-w-0 flex-col gap-3 text-sm">
          {questions.length ? questions.map((value, index) => {
            const question = record(value)
            const id = typeof question?.id === "string" ? question.id : null
            const prompt = typeof question?.question === "string" ? question.question : null
            const header = typeof question?.header === "string" ? question.header : null
            const answer = id && Array.isArray(answers?.[id]) ? answers[id].filter((value): value is string => typeof value === "string") : []
            return <div key={id ?? index} className="min-w-0">
              {header ? <p className="font-medium">{header}</p> : null}
              {prompt ? <MarkdownText text={prompt} token="" session={session} /> : <p>{t("inputResponseUnknownQuestion")}</p>}
              {state === "answered" && answer.length ? <div className="mt-1 rounded-md border bg-muted/30 px-3 py-2">
                <span className="text-muted-foreground">{t("inputResponseAnswer")}</span>
                {answer.map((text, answerIndex) => <MarkdownText key={answerIndex} text={text} token="" session={session} />)}
              </div> : null}
              {state === "awaiting" && Array.isArray(question?.options) ? <ul className="mt-1 list-inside list-disc text-muted-foreground">
                {question.options.map((value, optionIndex) => {
                  const option = record(value)
                  return typeof option?.label === "string" ? <li key={optionIndex}>{option.label}</li> : null
                })}
              </ul> : null}
            </div>
          }) : <p>{t("inputResponseUnknownQuestion")}</p>}
        </CardContent>
      </CollapsibleContent>
    </Card>
  </Collapsible>
}
