import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { JSDOM } from 'jsdom'
import { registerSource } from './helpers/onboarding-source.mjs'
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://fixture.example/', pretendToBeVisual: true })
for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'HTMLFormElement', 'HTMLButtonElement', 'Element', 'Node', 'NodeFilter', 'Event', 'CustomEvent', 'MutationObserver', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame', 'DocumentFragment']) Object.defineProperty(globalThis, name, { configurable: true, value: dom.window[name] })
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const { createElement: h, act } = await import('react')
const { createRoot } = await import('react-dom/client')
const { NextIntlClientProvider } = await import('next-intl')
const hooks = registerSource()
const contextHooks = registerHooks({ load(url, context, nextLoad) {
  if (url.endsWith('/components/auth/auth-context.tsx')) return { format: 'module', shortCircuit: true, source: 'export const useAuth = () => globalThis.adminAuth' }
  if (url.endsWith('/components/workspace-context.tsx')) return { format: 'module', shortCircuit: true, source: 'export const useWorkspace = () => globalThis.adminWorkspace' }
  return nextLoad(url, context)
} })
const { AdminPage } = await import('../src/components/admin/admin-page.tsx')
const { SettingsPage } = await import('../src/components/pages/settings-page.tsx')
const { authApi } = await import('../src/features/auth/api.ts')
const { announcementsApi } = await import('../src/features/announcements/api.ts')
contextHooks.deregister(); hooks.deregister()
const messages = JSON.parse(readFileSync(new URL('../messages/zh-CN.json', import.meta.url), 'utf8'))
const settings = { registrationOpen: true, oauthRegistrationOpen: true, passwordResetEnabled: false, oauth: null, email: { enabled: false, fromAddress: '', apiKeyConfigured: false } }
async function render(t, section, { role = 'admin', personal = false } = {}) {
  globalThis.adminAuth = { me: { userId: 'user', displayName: 'User', role, disabled: false }, session: { userId: 'user', accessToken: 'fixture-token' }, refreshConfig: async () => {}, refreshMe: async () => adminAuth.me }
  globalThis.adminWorkspace = { adminSection: section, settingsTab: section, navigate() {}, projects: [], sessions: [], isLoading: false, sidebarShowsSessions: false, sidebarCompactSessions: false, setSidebarShowsSessions() {}, setSidebarCompactSessions() {} }
  const container = document.createElement('div'); document.body.append(container)
  const root = createRoot(container)
  await act(async () => root.render(h(NextIntlClientProvider, { locale: 'zh-CN', messages, timeZone: 'Asia/Shanghai' }, h(personal ? SettingsPage : AdminPage))))
  t.after(async () => { await act(async () => root.unmount()); container.remove() })
  return container
}
test('a non-admin cannot mount a user management page or request its data', async (t) => {
  const users = t.mock.method(authApi, 'listUsers', async () => ({ users: [], total: 0 }))
  const container = await render(t, 'users', { role: 'user' })
  assert.equal(container.textContent, '')
  assert.equal(users.mock.callCount(), 0)
})
test('the dedicated announcement page saves publication changes without service settings reads', async (t) => {
  const service = t.mock.method(authApi, 'getSettings', async () => settings)
  t.mock.method(announcementsApi, 'settings', async () => ({ enabled: true, markdown: '公告内容', publishedAt: '2026-10-08T00:00:00Z' }))
  const save = t.mock.method(announcementsApi, 'save', async (_token, payload) => ({ ...payload, publishedAt: '2026-10-08T00:00:00Z' }))
  const container = await render(t, 'announcements')
  assert.equal(container.querySelector('h1').textContent, '公告')
  await act(async () => container.querySelector('[role="switch"]').click())
  await act(async () => container.querySelector('[data-slot="card-footer"] button').click())
  assert.equal(save.mock.callCount(), 1)
  assert.equal(save.mock.calls[0].arguments[1].enabled, false)
  assert.equal(service.mock.callCount(), 0)
  assert.match(container.textContent, /公告查看人数暂未提供/)
})
test('service configuration switches sections and updates the existing settings contract', async (t) => {
  t.mock.method(authApi, 'getSettings', async () => settings)
  t.mock.method(authApi, 'getServiceInfo', async () => ({ endpoint: 'https://fixture.example', version: '2.0.3', database: 'postgresql', databasePath: null, startedAt: '2026-10-08T00:00:00Z', uptimeSeconds: 60, serverTime: '2026-10-08T00:01:00Z' }))
  const update = t.mock.method(authApi, 'updateSettings', async (_token, patch) => ({ ...settings, ...patch }))
  const announcement = t.mock.method(announcementsApi, 'settings', async () => { throw new Error('Announcements have their own page') })
  const container = await render(t, 'settings')
  assert.equal(container.querySelector('h1').textContent, '服务配置')
  await act(async () => container.querySelector('[role="switch"]').click())
  assert.deepEqual(update.mock.calls[0].arguments[1], { registrationOpen: false })
  const info = [...container.querySelectorAll('button')].find(button => button.textContent === '服务信息')
  await act(async () => info.click())
  assert.match(container.textContent, /postgresql/)
  assert.equal(announcement.mock.callCount(), 0)
})
test('personal appearance settings retain controls without an inner navigation', async (t) => {
  t.mock.method(authApi, 'me', async () => adminAuth.me)
  const container = await render(t, 'appearance', { personal: true })
  assert.equal(container.querySelector('h1').textContent, '外观')
  assert.ok(container.querySelector('[role="radiogroup"]'))
  assert.equal(container.querySelector('nav'), null)
})
