"use client"

import { useTranslations } from "next-intl"
import { useWorkspace } from "@/components/workspace-context"
import { WorkspaceSectionSidebar } from "@/components/workspace-section-sidebar"
import { parseSettingsTab, settingsNavigation } from "@/components/settings/settings-navigation"

export function SettingsSidebar({ contained = false }: { contained?: boolean }) {
  const t = useTranslations("pages.settings")
  const { settingsTab, navigate } = useWorkspace()
  return (
    <WorkspaceSectionSidebar
      contained={contained}
      title={t("title")}
      activeId={parseSettingsTab(settingsTab)}
      onNavigate={(tab) => navigate("settings", tab)}
      groups={[
        {
          id: "personal",
          items: settingsNavigation
            .filter((item) => !["desktop", "startup", "logs"].includes(item.id))
            .map((item) => ({ ...item, label: t(item.labelKey) })),
        },
        {
          id: "desktop",
          label: t("desktop"),
          items: settingsNavigation
            .filter((item) => ["desktop", "startup", "logs"].includes(item.id))
            .map((item) => ({ ...item, label: t(item.labelKey) })),
        },
      ]}
    />
  )
}
