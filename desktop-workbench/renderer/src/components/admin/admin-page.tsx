"use client"

import { History } from "lucide-react"
import { useTranslations } from "next-intl"
import { useAuth } from "@/components/auth/auth-context"
import { useWorkspace } from "@/components/workspace-context"
import { DashboardPage } from "@/components/pages/dashboard-page"
import { TeamPage } from "@/components/pages/team-page"
import { ServicePage } from "@/components/pages/service-page"
import { ServiceAnnouncementCard } from "@/components/pages/service-announcement-card"
import { PageHeader } from "@/components/pages/page-header"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"

export function AdminPage() {
  const { adminSection } = useWorkspace()
  const { session, me } = useAuth()
  const t = useTranslations("admin")
  if (me?.role !== "admin") return null

  if (adminSection === "overview" || adminSection === "usage" || adminSection === "devices") {
    return <DashboardPage view={adminSection} />
  }
  if (adminSection === "users") return <TeamPage admin />
  if (adminSection === "settings") return <ServicePage admin />

  return (
    <ScrollArea className="h-full w-full bg-background">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-5 pb-16 pt-14 sm:px-8">
        <PageHeader title={t(`nav.${adminSection}`)} description={t(`descriptions.${adminSection}`)} />
        {adminSection === "announcements" ? (
          <>
            {session?.accessToken && <ServiceAnnouncementCard token={session.accessToken} />}
            <Alert>
              <AlertDescription>{t("announcementStatsPending")}</AlertDescription>
            </Alert>
          </>
        ) : (
          <Empty className="min-h-72 border">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <History />
              </EmptyMedia>
              <EmptyTitle>{t("recordsPendingTitle")}</EmptyTitle>
              <EmptyDescription>{t("recordsPendingDescription")}</EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
      </div>
    </ScrollArea>
  )
}
