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
        { id: "settings", items: settingsNavigation.map((item) => ({ ...item, label: t(item.labelKey) })) },
      ]}
    />
  )
}
