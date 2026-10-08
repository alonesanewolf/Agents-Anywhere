"use client"

import { ArrowLeft, type LucideIcon } from "lucide-react"
import { useTranslations } from "next-intl"
import { useAuth } from "@/components/auth/auth-context"
import { useWorkspace } from "@/components/workspace-context"
import { TooltipProvider } from "@/components/ui/tooltip"
import { SidebarAccountFooter } from "@/components/sidebar/sidebar-account-footer"
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"

export type SectionNavigationGroup = {
  id: string
  label?: string
  items: { id: string; label: string; icon: LucideIcon }[]
}

export function WorkspaceSectionSidebar({
  contained = false,
  title,
  groups,
  activeId,
  onNavigate,
}: {
  contained?: boolean
  title: string
  groups: SectionNavigationGroup[]
  activeId: string
  onNavigate: (id: string) => void
}) {
  const t = useTranslations("common")
  const { me, signOut } = useAuth()
  const { navigate, goHome } = useWorkspace()
  const { setOpenMobile } = useSidebar()

  return (
    <TooltipProvider>
      <Sidebar contained={contained} className="border-sidebar-border">
        <SidebarHeader className="px-4 pb-2 pt-4">
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                tooltip={t("backHome")}
                onClick={() => {
                  goHome()
                  setOpenMobile(false)
                }}
              >
                <ArrowLeft />
                <span>{t("backHome")}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
          <p className="px-2 text-sm font-medium text-muted-foreground">{title}</p>
        </SidebarHeader>
        <SidebarContent className="px-2">
          {groups.map(({ id, label, items }) => (
            <SidebarGroup key={id}>
              {label && <SidebarGroupLabel>{label}</SidebarGroupLabel>}
              <SidebarGroupContent>
                <SidebarMenu>
                  {items.map(({ id: itemId, label: itemLabel, icon: Icon }) => (
                    <SidebarMenuItem key={itemId}>
                      <SidebarMenuButton
                        isActive={activeId === itemId}
                        aria-current={activeId === itemId ? "page" : undefined}
                        tooltip={itemLabel}
                        onClick={() => {
                          onNavigate(itemId)
                          setOpenMobile(false)
                        }}
                      >
                        <Icon />
                        <span>{itemLabel}</span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          ))}
        </SidebarContent>
        <SidebarAccountFooter me={me} navigate={navigate} signOut={signOut} />
      </Sidebar>
    </TooltipProvider>
  )
}
