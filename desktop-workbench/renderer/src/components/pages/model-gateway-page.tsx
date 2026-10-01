"use client"

import { ArrowRight, Eye, EyeOff } from "lucide-react"
import { useTranslations } from "next-intl"
import { toast } from "sonner"

import { DashboardSidebarToggle } from "@/components/dashboard-sidebar-toggle"
import { OnboardingShell } from "@/components/onboarding/reference/components/onboarding-shell"
import { SlideFrame } from "@/components/onboarding/reference/components/slide-frame"
import { Button as SlideButton } from "@/components/onboarding/reference/components/ui/button"
import { Button } from "@/components/ui/button"
import { ModelGatewayArtwork } from "@/features/model-gateway/artwork"
import { useModelGatewaySidebarVisibility } from "@/features/model-gateway/sidebar-visibility"
import { PRODUCT_LINKS } from "@/lib/product-links"
import "@/components/onboarding/reference/styles/onboarding.css"

export function ModelGatewayPage() {
  const t = useTranslations("dashboard.modelGateway")
  const [sidebarVisible, setSidebarVisible] = useModelGatewaySidebarVisibility()

  const toggleSidebarVisibility = () => {
    const nextVisible = !sidebarVisible
    setSidebarVisible(nextVisible)
    toast.success(t(nextVisible ? "shownToast" : "hiddenToast"))
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col bg-background">
      <div className="absolute left-3 top-3.5 z-10">
        <DashboardSidebarToggle showOnDesktop />
      </div>
      <div className="absolute right-3 top-3 z-10 rounded-md bg-background">
        <Button type="button" variant="ghost" size="sm" onClick={toggleSidebarVisibility}>
          {sidebarVisible ? <EyeOff data-icon="inline-start" /> : <Eye data-icon="inline-start" />}
          {t(sidebarVisible ? "hideFromSidebar" : "showInSidebar")}
        </Button>
      </div>

      <div className="onboarding-viewport onboarding-viewport-embedded">
        <OnboardingShell artwork="phone" wordmark={false}>
          <div className="slide-page">
            <SlideFrame
              id="model-gateway"
              title={t("headline")}
              description={t("description")}
              artwork={<div className="-mx-6"><ModelGatewayArtwork /></div>}
              actions={PRODUCT_LINKS.modelGatewayUrl ? (
                <SlideButton asChild size="lg">
                  <a href={PRODUCT_LINKS.modelGatewayUrl} target="_blank" rel="noopener noreferrer">
                    {t("openDashboard")}<ArrowRight data-icon="inline-end" />
                  </a>
                </SlideButton>
              ) : <p className="text-sm text-muted-foreground">{t("unavailable")}</p>}
            />
          </div>
        </OnboardingShell>
      </div>
    </div>
  )
}
