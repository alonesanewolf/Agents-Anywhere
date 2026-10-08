import { Archive, Smartphone, Sun, User } from "lucide-react"

export const settingsNavigation = [
  { id: "account", labelKey: "account", icon: User },
  { id: "appearance", labelKey: "appearance", icon: Sun },
  { id: "mobile-connections", labelKey: "mobileConnections", icon: Smartphone },
  { id: "archived-sessions", labelKey: "archivedSessions", icon: Archive },
] as const

export type SettingsTab = (typeof settingsNavigation)[number]["id"]

export function parseSettingsTab(value: string): SettingsTab {
  return settingsNavigation.find((item) => item.id === value)?.id ?? "account"
}
