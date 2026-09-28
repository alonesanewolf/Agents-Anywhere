import type { NextConfig } from "next";

const apiTarget = process.env.AGENTS_ANYWHERE_API ?? "http://127.0.0.1:8000";
const apiNamespace = process.env.AGENTS_ANYWHERE_API_NAMESPACE ?? "/api/v2";
const proxyClientMaxBodySize = 100 * 1024 * 1024;
const staticExport = process.env.NEXT_OUTPUT === "export";
const browserApiTarget = staticExport ? "" : apiTarget;
const apiRoutePrefixes = [
  "/admin",
  "/agents",
  "/auth",
  "/connector",
  "/connectors",
  "/health",
  "/oauth",
  "/pairing",
  "/sessions",
  "/.well-known",
];

const nextConfig: NextConfig = {
  devIndicators: false,
  allowedDevOrigins: ["**.*", "localhost", "*.localhost"],
  output: staticExport ? "export" : undefined,
  trailingSlash: staticExport,
  env: {
    NEXT_PUBLIC_AGENTS_ANYWHERE_API: browserApiTarget,
    NEXT_PUBLIC_AGENTS_ANYWHERE_API_NAMESPACE: apiNamespace,
  },
  experimental: {
    // Full timeline sync uses HTTP ingest through this same-origin proxy.
    proxyClientMaxBodySize,
    ...(!staticExport ? { proxyTimeout: apiProxyTimeout() } : {}),
  },
  ...(staticExport
    ? {}
    : {
        async rewrites() {
          const namespace = normalizeApiNamespace(apiNamespace);
          if (namespace) {
            const source = `${namespace}/:path*`;
            return [{ source, destination: `${apiTarget}${source}` }];
          }
          return apiRoutePrefixes.map((prefix) => ({
            source: `${prefix}/:path*`,
            destination: `${apiTarget}${prefix}/:path*`
          }));
        }
      })
};

function normalizeApiNamespace(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "/") return "";
  return `/${trimmed.replace(/^\/+|\/+$/g, "")}`;
}

function apiProxyTimeout(): number {
  const overrideName = "AGENTS_ANYWHERE_API_PROXY_TIMEOUT_MS";
  const readName = "AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS";
  const override = process.env[overrideName];
  const rawRead = process.env[readName];
  const read = rawRead === undefined ? 20 : Number(rawRead);
  const value = override === undefined ? Math.ceil(1000 * (2 * read + 60)) : Number(override);
  const name = override === undefined ? readName : overrideName;
  if ((override === undefined && (!Number.isFinite(read) || read <= 0)) ||
      !Number.isSafeInteger(value) || value < 1 || value > 2147483647) {
    throw new Error(`${name} must specify a positive finite budget within the Node timer range`);
  }
  return value;
}

export default nextConfig;
