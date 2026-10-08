import { Archive, Laptop, Logs, Rocket, Smartphone, Sun, User } from "lucide-react"

export const settingsNavigation = [
  { id: "account", labelKey: "account", icon: User },
  { id: "appearance", labelKey: "appearance", icon: Sun },
  { id: "mobile-connections", labelKey: "mobileConnections", icon: Smartphone },
  { id: "archived-sessions", labelKey: "archivedSessions", icon: Archive },
  { id: "desktop", labelKey: "desktop", icon: Laptop },
  { id: "startup", labelKey: "startup", icon: Rocket },
  { id: "logs", labelKey: "logs", icon: Logs },
] as const

export type SettingsTab = (typeof settingsNavigation)[number]["id"]
export function parseSettingsTab(value: string): SettingsTab {
  return settingsNavigation.find((item) => item.id === value)?.id ?? "account"
}
