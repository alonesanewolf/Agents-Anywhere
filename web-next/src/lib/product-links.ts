/** Fixed product configuration. Empty URLs remain visibly unavailable. */
export const PRODUCT_LINKS: {
  modelGatewayUrl: string
  desktopDownloadUrl: string
  landingPageUrl: string
  downloadPageUrl: string
  androidDownloadUrl: string
  iosDownloadUrl: string
  webAppHref: string
} = {
  // Set NEXT_PUBLIC_MODEL_GATEWAY_URL for deployed builds; local OAuth uses HTTPS.
  modelGatewayUrl: process.env.NEXT_PUBLIC_MODEL_GATEWAY_URL ?? (process.env.NODE_ENV === 'development' ? 'https://localhost:8443/dashboard' : ''),
  desktopDownloadUrl: '',
  landingPageUrl: '',
  downloadPageUrl: 'https://agents-anywhere.com/download',
  androidDownloadUrl: 'https://github.com/anywhere-labs/Agents-Anywhere/releases/latest',
  iosDownloadUrl: '',
  webAppHref: '#/',
}
