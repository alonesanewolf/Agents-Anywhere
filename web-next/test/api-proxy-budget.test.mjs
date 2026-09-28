import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';
const source = ts.transpileModule(readFileSync(new URL('../next.config.ts', import.meta.url), 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
function config(env={}) { const c = vm.createContext({exports:{}, process:{env}}); vm.runInContext(source,c); return c.exports.default; }
test('outer budget follows backend allocation and honors explicit shorter override', () => {
 assert.equal(config().experimental.proxyTimeout,100000);
 assert.equal(config({AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS:'25.0001'}).experimental.proxyTimeout,110001);
 assert.equal(config({AGENTS_ANYWHERE_API_PROXY_TIMEOUT_MS:'25',AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS:'bad'}).experimental.proxyTimeout,25);
});
test('static export ignores proxy environment; namespaces stay configurable', async () => {
 const c=config({NEXT_OUTPUT:'export',AGENTS_ANYWHERE_API_PROXY_TIMEOUT_MS:'bad'});
 assert.equal(c.experimental.proxyTimeout,undefined); assert.equal(c.rewrites,undefined);
 for (const namespace of ['/api/v2','/custom/','/','']) {
  const rewrites=await config({AGENTS_ANYWHERE_API_NAMESPACE:namespace}).rewrites();
  assert.equal(rewrites[0].source,namespace.includes('api')?'/api/v2/:path*':namespace.includes('custom')?'/custom/:path*':'/admin/:path*');
 }
});
for (const value of ['0','-1','NaN','Infinity','1.2','2147483648','']) test(`reject explicit proxy bound ${value}`,()=>assert.throws(()=>config({AGENTS_ANYWHERE_API_PROXY_TIMEOUT_MS:value}),/AGENTS_ANYWHERE_API_PROXY_TIMEOUT_MS/));
for (const value of ['0','-1','NaN','Infinity','2147483647','']) test(`reject derived bound ${value}`,()=>assert.throws(()=>config({AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS:value}),/AGENT_SERVER_SESSION_RPC_TIMEOUT_SECONDS/));
