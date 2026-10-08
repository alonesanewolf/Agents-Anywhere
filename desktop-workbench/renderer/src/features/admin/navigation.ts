export const adminSections = [
  "overview",
  "usage",
  "devices",
  "users",
  "announcements",
  "settings",
  "records",
] as const

export type AdminSection = (typeof adminSections)[number]

export function parseAdminSection(value?: string): AdminSection {
  return adminSections.find((section) => section === value) ?? "overview"
}

export const adminNavigation = [
  { group: "data", sections: ["overview", "usage", "devices"] },
  { group: "management", sections: ["users", "announcements", "settings", "records"] },
] as const
