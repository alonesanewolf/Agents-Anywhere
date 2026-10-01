"use client"

import { createSidebarVisibility } from "../sidebar/sidebar-visibility"

const visibility = createSidebarVisibility(
  "aa-model-gateway-sidebar-visible-v1",
  "aa:model-gateway-sidebar-visibility",
)

export const useModelGatewaySidebarVisibility = visibility.useVisibility
