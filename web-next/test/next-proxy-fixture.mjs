// Private test listener using the installed Next proxy, never a live Next app.
import http from "node:http";
import { parse } from "node:url";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
const require = createRequire(import.meta.url);
const { proxyRequest } = require("next/dist/server/lib/router-utils/proxy-request.js");
const [upstream, mode, scale] = process.argv.slice(2);
const env = mode === "short" ? { AGENTS_ANYWHERE_API_PROXY_TIMEOUT_MS: "20000" } : {};
const source = ts.transpileModule(readFileSync(new URL("../next.config.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;
const context = vm.createContext({ exports: {}, process: { env } });
vm.runInContext(source, context);
// Legacy 30000 is the installed proxy's documented default, scaled only here.
const timeout = (mode === "legacy" ? 30000 : context.exports.default.experimental.proxyTimeout) * Number(scale);
const server = http.createServer((req, res) => {
  proxyRequest(req, res, parse(upstream + req.url, true), undefined, undefined, timeout).catch(() => {});
});
server.listen(0, "127.0.0.1", () => process.stdout.write(`${server.address().port}\n`));
process.on("SIGTERM", () => { server.closeAllConnections(); server.close(() => process.exit(0)); });
