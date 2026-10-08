"use client"

import { BarChart3, History, LayoutDashboard, Megaphone, Monitor, Settings2, Users } from "lucide-react"
import { useTranslations } from "next-intl"
import { useWorkspace } from "@/components/workspace-context"
import { WorkspaceSectionSidebar } from "@/components/workspace-section-sidebar"
import { adminNavigation, parseAdminSection, type AdminSection } from "@/features/admin/navigation"

const icons = {
  overview: LayoutDashboard,
  usage: BarChart3,
  devices: Monitor,
  users: Users,
  announcements: Megaphone,
  settings: Settings2,
  records: History,
} satisfies Record<AdminSection, typeof LayoutDashboard>

export function AdminSidebar({ contained = false }: { contained?: boolean }) {
  const t = useTranslations("admin")
  const { adminSection, navigate } = useWorkspace()
  return (
    <WorkspaceSectionSidebar
      contained={contained}
      title={t("title")}
      activeId={adminSection}
      onNavigate={(section) => navigate("admin", parseAdminSection(section))}
      groups={adminNavigation.map(({ group, sections }) => ({
        id: group,
        label: t(`groups.${group}`),
        items: sections.map((id) => ({ id, label: t(`nav.${id}`), icon: icons[id] })),
      }))}
    />
  )
}
