import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { JSDOM } from 'jsdom'
import { registerSource } from './helpers/onboarding-source.mjs'

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://app.example.test/', pretendToBeVisual: true })
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'HTMLFormElement', 'Element', 'Node', 'Event', 'CustomEvent', 'MutationObserver', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame']) {
  Object.defineProperty(globalThis, key, { configurable: true, value: dom.window[key] })
}
globalThis.ResizeObserver = class { observe() {} disconnect() {} }
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const hook = registerSource()
const { createElement: h, act, useState } = await import('react')
const { createRoot } = await import('react-dom/client')
const { NextIntlClientProvider } = await import('next-intl')
const { SessionComposer } = await import('../src/components/session/session-composer.tsx')
const { dashboardApi } = await import('../src/features/dashboard/api.ts')
hook.deregister()
const messages = JSON.parse(readFileSync(new URL('../messages/en.json', import.meta.url), 'utf8'))
const session = { id: 's1', runtime: 'codex', runtimeId: 'codex', externalSessionId: 'thread-1', connectorStatus: 'online', status: 'idle', archived: false, takeover: true }
const capability = (...names) => ({ revision: 1, capabilities: names.map((capabilityId) => ({ capabilityId, scope: 'session', runtime: 'codex', runtimeId: 'codex', sessionId: 's1', supported: true, available: true, allowed: true })) })
const modelCatalog = { models: [{ id: 'astra', displayName: 'Astra', description: null, selectionId: 'model:astra', default: true, enabled: true, reasoningItems: [], metadata: {} }] }
const permissionCatalog = { permissions: [{ id: 'request', displayName: 'Request approval', description: null, selectionId: 'permission:request', default: true, enabled: true, metadata: {} }] }
const textSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set

async function mount(t, child) {
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  await act(async () => root.render(h(NextIntlClientProvider, { locale: 'en', messages, timeZone: 'UTC' }, child)))
  t.after(async () => { await act(async () => root.unmount()); host.remove() })
  return host
}

async function type(input, value) {
  await act(async () => {
    textSetter.call(input, value)
    input.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
}

function props(overrides = {}) {
  return {
    token: 'test', session, runtimeState: { status: 'running', selections: {}, metadata: {} },
    pendingInteractionCount: 0, sending: false, interrupting: false, takeoverBusy: false,
    value: 'steer this work', effectiveCapabilities: capability('session.steer', 'session.interrupt', 'session.send_message'),
    modelCatalog: null, permissionCatalog: null, runtimeCommands: [],
    onCommandQueryChange() {}, onValueChange() {}, onSelectionChange: async () => true,
    onSend: async () => assert.fail('running steer must not use normal send'),
    onInterrupt() {}, onCommand: async () => assert.fail('ordinary text must not run a command'), onToggleTakeover() {},
    ...overrides,
  }
}

test('running steer can be sent by click and Enter while Stop stays independently reachable', async t => {
  const steers = []
  let stopped = 0
  function Host() {
    const [value, setValue] = useState('steer this work')
    return h(SessionComposer, props({ value, onValueChange: setValue, onSteer: async (text, files) => { steers.push({ text, files }); return { ok: true, state: 'accepted' } }, onInterrupt: () => { stopped++ } }))
  }
  const host = await mount(t, h(Host))
  const input = host.querySelector('textarea')
  const steer = host.querySelector('button[aria-label="Send while running"]')
  const stop = host.querySelector('button[aria-label="Interrupt"]')
  assert.ok(steer)
  assert.ok(stop)
  await act(async () => steer.click())
  assert.deepEqual(steers.map(({ text }) => text), ['steer this work'])
  assert.equal(stopped, 0)
  await type(input, 'second steer')
  await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  assert.deepEqual(steers.map(({ text }) => text), ['steer this work', 'second steer'])
  await act(async () => stop.click())
  assert.equal(stopped, 1)
})

test('unsupported steer retains the draft and never falls back to a normal turn', async t => {
  let sent = 0
  const host = await mount(t, h(SessionComposer, props({ effectiveCapabilities: capability('session.interrupt', 'session.send_message'), onSend: async () => { sent++; return true } })))
  const input = host.querySelector('textarea')
  assert.equal(host.querySelector('button[aria-label="Send while running"]'), null)
  await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  assert.equal(sent, 0)
  assert.equal(input.value, 'steer this work')
  assert.ok(host.querySelector('button[aria-label="Interrupt"]'))
})

test('existing unobserved selections stay inherited on ordinary Send despite catalog defaults', async t => {
  const writes = []
  const sends = []
  function Host() {
    const [value, setValue] = useState('resume native chat')
    return h(SessionComposer, props({
      runtimeState: { status: 'idle', selections: {}, metadata: {} }, value, onValueChange: setValue,
      effectiveCapabilities: capability('session.send_message', 'catalog.model', 'catalog.permission', 'catalog.effort'),
      modelCatalog, permissionCatalog,
      onSelectionChange: async selection => { writes.push(selection); return true },
      onSend: async (text, _files, selections) => { sends.push({ text, selections }); if (Object.keys(selections).length) writes.push(selections); return true },
    }))
  }
  const host = await mount(t, h(Host))
  assert.match(host.textContent, /Current setting unknown/)
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.deepEqual(sends, [{ text: 'resume native chat', selections: {} }])
  assert.deepEqual(writes, [])
})

test('observed selections becoming unknown in the same session are not submitted as overrides', async t => {
  const sends = []
  let changes = 0
  function Host() {
    const [selections, setSelections] = useState({ model: 'model:astra', permission: 'permission:request' })
    const [value, setValue] = useState('resume')
    window.loseObservedSettings = () => setSelections({})
    return h(SessionComposer, props({
      runtimeState: { status: 'idle', selections, metadata: {} }, value, onValueChange: setValue,
      effectiveCapabilities: capability('session.send_message', 'catalog.model', 'catalog.permission'),
      modelCatalog, permissionCatalog,
      onSelectionChange: async () => { changes++; return true },
      onSend: async (_text, _files, selected) => { sends.push(selected); return true },
    }))
  }
  const host = await mount(t, h(Host))
  assert.match(host.textContent, /Astra/)
  assert.match(host.textContent, /Request approval/)
  await act(async () => window.loseObservedSettings())
  assert.match(host.textContent, /Current setting unknown/)
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.deepEqual(sends, [{}])
  assert.equal(changes, 0)
})

test('deliberate model and permission changes remain selected when observation becomes unknown', async t => {
  const writes = []
  const sends = []
  const models = { models: [...modelCatalog.models, { ...modelCatalog.models[0], id: 'nova', displayName: 'Nova', selectionId: 'model:nova', default: false }] }
  const permissions = { permissions: [...permissionCatalog.permissions, { ...permissionCatalog.permissions[0], id: 'grant', displayName: 'Full access', selectionId: 'permission:grant', default: false }] }
  function Host() {
    const [selections, setSelections] = useState({ model: 'model:astra', permission: 'permission:request' })
    const [value, setValue] = useState('resume')
    window.loseObservedSettings = () => setSelections({})
    return h(SessionComposer, props({
      runtimeState: { status: 'idle', selections, metadata: {} }, value, onValueChange: setValue,
      effectiveCapabilities: capability('session.send_message', 'catalog.model', 'catalog.permission'),
      modelCatalog: models, permissionCatalog: permissions,
      onSelectionChange: async selection => { writes.push(selection); return true },
      onSend: async (_text, _files, selected) => { sends.push(selected); return true },
    }))
  }
  const host = await mount(t, h(Host))
  const choose = async label => {
    const trigger = [...host.querySelectorAll('button[aria-haspopup="menu"]')].find(button => button.textContent.includes(label === 'Nova' ? 'Astra' : 'Request approval'))
    assert.ok(trigger)
    await act(async () => trigger.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0 })))
    const item = [...document.querySelectorAll('[role="menuitem"]')].find(element => element.textContent.includes(label))
    assert.ok(item)
    await act(async () => item.click())
  }
  await choose('Nova')
  await choose('Full access')
  assert.deepEqual(writes, [{ model: 'model:nova' }, { permission: 'permission:grant' }])
  await act(async () => window.loseObservedSettings())
  assert.match(host.textContent, /Nova/)
  assert.match(host.textContent, /Full access/)
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.deepEqual(sends, [{ model: 'model:nova', permission: 'permission:grant' }])
})

test('acknowledged and observed choices yield to later authoritative native settings', async t => {
  const sends = []
  const models = { models: [...modelCatalog.models, { ...modelCatalog.models[0], id: 'nova', displayName: 'Nova', selectionId: 'model:nova', default: false }] }
  const permissions = { permissions: [...permissionCatalog.permissions, { ...permissionCatalog.permissions[0], id: 'grant', displayName: 'Full access', selectionId: 'permission:grant', default: false }] }
  function Host() {
    const [selections, setSelections] = useState({ model: 'model:astra', permission: 'permission:request' })
    const [value, setValue] = useState('continue')
    window.observeLaterNativeSettings = () => setSelections({ model: 'model:astra', permission: 'permission:request' })
    return h(SessionComposer, props({
      runtimeState: { status: 'idle', selections, metadata: {} }, value, onValueChange: setValue,
      effectiveCapabilities: capability('session.send_message', 'catalog.model', 'catalog.permission'),
      modelCatalog: models, permissionCatalog: permissions,
      onSelectionChange: async selection => { setSelections(current => ({ ...current, ...selection })); return true },
      onSend: async (_text, _files, selected) => { sends.push(selected); return true },
    }))
  }
  const host = await mount(t, h(Host))
  const choose = async (current, target) => {
    const trigger = [...host.querySelectorAll('button[aria-haspopup="menu"]')].find(button => button.textContent.includes(current))
    assert.ok(trigger)
    await act(async () => trigger.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0 })))
    const item = [...document.querySelectorAll('[role="menuitem"]')].find(element => element.textContent.includes(target))
    assert.ok(item)
    await act(async () => item.click())
  }
  await choose('Astra', 'Nova')
  await choose('Request approval', 'Full access')
  await act(async () => window.observeLaterNativeSettings())
  assert.match(host.textContent, /Astra/)
  assert.match(host.textContent, /Request approval/)
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.deepEqual(sends, [{ model: 'model:astra', permission: 'permission:request' }])
})

test('pending permission intent survives observations until acknowledgement, then yields to native', async t => {
  let acknowledge
  const sends = []
  const permissions = { permissions: [...permissionCatalog.permissions, { ...permissionCatalog.permissions[0], id: 'grant', displayName: 'Full access', selectionId: 'permission:grant', default: false }] }
  function Host() {
    const [selections, setSelections] = useState({ permission: 'permission:request' })
    const [value, setValue] = useState('continue')
    window.observeAcceptedPermission = () => setSelections({ permission: 'permission:grant' })
    window.observeNativeTightening = () => setSelections({ permission: 'permission:request' })
    return h(SessionComposer, props({
      runtimeState: { status: 'idle', selections, metadata: {} }, value, onValueChange: setValue,
      effectiveCapabilities: capability('session.send_message', 'catalog.permission'),
      permissionCatalog: permissions,
      onSelectionChange: () => new Promise(resolve => { acknowledge = resolve }),
      onSend: async (_text, _files, selected) => { sends.push(selected); return true },
    }))
  }
  const host = await mount(t, h(Host))
  const trigger = host.querySelector('button[aria-haspopup="menu"]')
  await act(async () => trigger.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0 })))
  const item = [...document.querySelectorAll('[role="menuitem"]')].find(element => element.textContent.includes('Full access'))
  assert.ok(item)
  await act(async () => item.click())
  assert.ok(acknowledge)
  await act(async () => window.observeAcceptedPermission())
  await act(async () => window.observeNativeTightening())
  assert.match(host.textContent, /Full access/)
  await act(async () => acknowledge(true))
  assert.match(host.textContent, /Request approval/)
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.deepEqual(sends, [{ permission: 'permission:request' }])
})

test('an explicit model choice writes settings while an untouched permission remains inherited', async t => {
  const writes = []
  const sent = []
  function Host() {
    const [value, setValue] = useState('resume')
    return h(SessionComposer, props({
      runtimeState: { status: 'idle', selections: {}, metadata: {} }, value, onValueChange: setValue,
      effectiveCapabilities: capability('session.send_message', 'catalog.model', 'catalog.permission'),
      modelCatalog, permissionCatalog,
      onSelectionChange: async selection => { writes.push(selection); return true },
      onSend: async (_text, _files, selections) => { sent.push(selections); return true },
    }))
  }
  const host = await mount(t, h(Host))
  const triggers = host.querySelectorAll('button[aria-haspopup="menu"]')
  assert.ok(triggers.length >= 2)
  try {
    await act(async () => triggers[1].dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0 })))
  } catch (error) {
    throw error.errors?.[0] ?? error
  }
  const item = [...document.querySelectorAll('[role="menuitem"]')].find(element => element.textContent.includes('Astra'))
  assert.ok(item)
  await act(async () => item.click())
  assert.deepEqual(writes, [{ model: 'model:astra' }])
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.deepEqual(sent, [{ model: 'model:astra' }])
})

test('late selector rejection from A cannot erase B observed setting', async t => {
  let finishA
  function Host() {
    const [selected, setSelected] = useState(session)
    window.visitSelectedB = () => setSelected({ ...session, id: 's2' })
    return h(SessionComposer, props({
      session: selected,
      runtimeState: { status: 'idle', selections: selected.id === 's2' ? { model: 'model:astra' } : {}, metadata: {} },
      value: '', effectiveCapabilities: capability('catalog.model'), modelCatalog,
      onSelectionChange: () => new Promise(resolve => { finishA = resolve }),
    }))
  }
  const host = await mount(t, h(Host))
  const trigger = host.querySelector('button[aria-haspopup="menu"]')
  await act(async () => trigger.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0 })))
  const item = [...document.querySelectorAll('[role="menuitem"]')].find(element => element.textContent.includes('Astra'))
  assert.ok(item)
  await act(async () => item.click())
  assert.ok(finishA)
  await act(async () => window.visitSelectedB())
  assert.match(host.textContent, /Astra/)
  await act(async () => finishA(false))
  assert.match(host.textContent, /Astra/)
})

test('explicit selector intent from A does not leak into unobserved B ordinary send', async t => {
  const sends = []
  function Host() {
    const [selected, setSelected] = useState(session)
    const [value, setValue] = useState('resume')
    window.visitUnobservedB = () => setSelected({ ...session, id: 's2' })
    return h(SessionComposer, props({
      session: selected, runtimeState: { status: 'idle', selections: {}, metadata: {} },
      value, onValueChange: setValue,
      effectiveCapabilities: capability('session.send_message', 'catalog.model'), modelCatalog,
      onSelectionChange: async () => true,
      onSend: async (_text, _files, selections) => { sends.push(selections); return true },
    }))
  }
  const host = await mount(t, h(Host))
  const trigger = host.querySelector('button[aria-haspopup="menu"]')
  await act(async () => trigger.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0 })))
  const item = [...document.querySelectorAll('[role="menuitem"]')].find(element => element.textContent.includes('Astra'))
  assert.ok(item)
  await act(async () => item.click())
  await act(async () => window.visitUnobservedB())
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.deepEqual(sends, [{}])
})

test('native rejection and unknown delivery keep the raw draft visible without normal-send fallback', async t => {
  for (const outcome of [
    { ok: false, state: 'rejected', message: 'native refused' },
    { ok: false, state: 'unknown', message: 'connection lost' },
  ]) {
    let sent = 0
    const host = await mount(t, h(SessionComposer, props({
      value: '  keep exact whitespace  ',
      onSend: async () => { sent++; return true },
      onSteer: async raw => { assert.equal(raw, '  keep exact whitespace  '); return outcome },
    })))
    await act(async () => host.querySelector('button[aria-label="Send while running"]').click())
    assert.equal(sent, 0)
    assert.equal(host.querySelector('textarea').value, '  keep exact whitespace  ')
    assert.match(host.querySelector('[role="alert"]').textContent, new RegExp(outcome.message))
  }
})

test('pending steer blocks double submit and preserves a draft edited before acknowledgement', async t => {
  const pending = []
  function Host() {
    const [value, setValue] = useState('first text')
    return h(SessionComposer, props({ value, onValueChange: setValue, onSteer: () => new Promise(resolve => pending.push(resolve)) }))
  }
  const host = await mount(t, h(Host))
  const input = host.querySelector('textarea')
  await act(async () => {
    input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  })
  assert.equal(pending.length, 1)
  await type(input, 'edited while pending')
  await act(async () => pending[0]({ ok: true, state: 'accepted' }))
  assert.equal(input.value, 'edited while pending')
})

test('A to B to A visit ignores old completion and does not release the new request', async t => {
  const pending = []
  function Host() {
    const [selected, setSelected] = useState(session)
    const [value, setValue] = useState('A draft')
    window.visitB = () => { setSelected({ ...session, id: 's2' }); setValue('B draft') }
    window.visitA = () => { setSelected(session); setValue('A draft') }
    return h(SessionComposer, props({ session: selected, value, onValueChange: setValue, onSteer: () => new Promise(resolve => pending.push(resolve)) }))
  }
  const host = await mount(t, h(Host))
  const input = host.querySelector('textarea')
  const enter = async () => act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  await enter()
  await act(async () => window.visitB())
  await act(async () => window.visitA())
  await enter()
  assert.equal(pending.length, 2)
  await act(async () => pending[0]({ ok: true, state: 'accepted' }))
  assert.equal(input.value, 'A draft')
  await enter()
  assert.equal(pending.length, 2)
  await act(async () => pending[1]({ ok: true, state: 'accepted' }))
  assert.equal(input.value, '')
})

test('late ordinary send failure from A cannot restore its draft into B', async t => {
  let rejectA
  function Host() {
    const [selected, setSelected] = useState(session)
    const [value, setValue] = useState('A raw draft')
    window.visitOrdinaryB = () => { setSelected({ ...session, id: 's2' }); setValue('') }
    return h(SessionComposer, props({
      session: selected, runtimeState: { status: 'idle', selections: {}, metadata: {} },
      value, onValueChange: setValue, effectiveCapabilities: capability('session.send_message'),
      onSend: () => new Promise(resolve => { rejectA = resolve }),
    }))
  }
  const host = await mount(t, h(Host))
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.ok(rejectA)
  await act(async () => window.visitOrdinaryB())
  await act(async () => rejectA(false))
  assert.equal(host.querySelector('textarea').value, '')
})

test('a pending ordinary start blocks steer until its request settles', async t => {
  let acknowledge
  const steers = []
  function Host() {
    const [status, setStatus] = useState('idle')
    const [value, setValue] = useState('start work')
    window.observeRunning = () => setStatus('running')
    return h(SessionComposer, props({
      runtimeState: { status, selections: {}, metadata: {} }, value, onValueChange: setValue,
      onSend: () => new Promise(resolve => { acknowledge = resolve }),
      onSteer: async text => { steers.push(text); return { ok: true, state: 'accepted' } },
    }))
  }
  const host = await mount(t, h(Host))
  const input = host.querySelector('textarea')
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.ok(acknowledge)
  await act(async () => window.observeRunning())
  await type(input, 'add detail')
  await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  assert.deepEqual(steers, [])
  await act(async () => acknowledge(true))
  await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  assert.deepEqual(steers, ['add detail'])
})

test('pending steer cannot become an ordinary Send when the native turn becomes idle', async t => {
  let finishSteer
  const sends = []
  function Host() {
    const [status, setStatus] = useState('running')
    const [value, setValue] = useState('in-flight steer')
    window.finishNativeTurn = () => setStatus('idle')
    return h(SessionComposer, props({
      runtimeState: { status, selections: {}, metadata: {} }, value, onValueChange: setValue,
      onSteer: () => new Promise(resolve => { finishSteer = resolve }),
      onSend: async text => { sends.push(text); return true },
    }))
  }
  const host = await mount(t, h(Host))
  await act(async () => host.querySelector('button[aria-label="Send while running"]').click())
  await act(async () => window.finishNativeTurn())
  const ordinaryButton = host.querySelector('button[aria-label="Send"]')
  assert.ok(ordinaryButton)
  assert.equal(ordinaryButton.disabled, true)
  await act(async () => ordinaryButton.click())
  await act(async () => host.querySelector('textarea').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  assert.deepEqual(sends, [])
  await act(async () => finishSteer({ ok: false, state: 'rejected', message: 'turn finished' }))
  assert.equal(ordinaryButton.disabled, false)
  await act(async () => ordinaryButton.click())
  assert.deepEqual(sends, ['in-flight steer'])
})

test('running slash command uses its own route and leaves Stop independent', async t => {
  const calls = []
  let stopped = 0
  const descriptor = { id: 'goal', title: 'Goal', description: 'Native goal', aliases: [], scope: 'session', enabled: true, disabledReason: null, acceptsArgs: true, argsSchema: { type: 'string' }, metadata: { ui: { kind: 'execute', allowedStatuses: ['running'] } } }
  const host = await mount(t, h(SessionComposer, props({
    value: '/goal status', runtimeCommands: [descriptor],
    effectiveCapabilities: capability('session.steer', 'session.interrupt', 'session.commands'),
    onSteer: async () => assert.fail('slash intent must not steer'),
    onCommand: async (id, options) => { calls.push({ id, raw: options.raw }); return { ok: true, state: 'accepted', message: null } },
    onInterrupt: () => { stopped++ },
  })))
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.deepEqual(calls, [{ id: 'goal', raw: '/goal status' }])
  await act(async () => host.querySelector('button[aria-label="Interrupt"]').click())
  assert.equal(stopped, 1)
})

test('uploaded attachment accompanies steer and stays with draft on rejection', async t => {
  t.mock.method(dashboardApi, 'uploadSessionAttachments', async () => ({ attachments: [{ fileId: 'file-1', name: 'brief.txt', mediaType: 'text/plain', size: 3 }] }))
  const received = []
  const host = await mount(t, h(SessionComposer, props({
    effectiveCapabilities: capability('session.steer', 'session.interrupt', 'runtime.attachment'),
    onSteer: async (raw, files) => { received.push({ raw, files }); return { ok: false, state: 'rejected', message: 'not accepted' } },
  })))
  const file = new window.File(['abc'], 'brief.txt', { type: 'text/plain' })
  const paste = new window.Event('paste', { bubbles: true })
  Object.defineProperty(paste, 'clipboardData', { value: { items: [{ kind: 'file', getAsFile: () => file }] } })
  await act(async () => window.dispatchEvent(paste))
  assert.match(host.textContent, /brief.txt/)
  await act(async () => host.querySelector('button[aria-label="Send while running"]').click())
  assert.equal(received.length, 1)
  assert.equal(received[0].files[0].uploaded.fileId, 'file-1')
  assert.match(host.textContent, /brief.txt/)
  assert.equal(host.querySelector('textarea').value, 'steer this work')
})

test('acknowledged steer clears only the unchanged draft and uploaded attachment set', async t => {
  t.mock.method(dashboardApi, 'uploadSessionAttachments', async (_token, _sessionId, files) => ({
    attachments: [{ fileId: `uploaded-${files[0].name}`, name: files[0].name, mediaType: 'text/plain', size: 3 }],
  }))
  const pending = []
  function Host() {
    const [value, setValue] = useState('first steer')
    return h(SessionComposer, props({
      value, onValueChange: setValue,
      effectiveCapabilities: capability('session.steer', 'session.interrupt', 'runtime.attachment'),
      onSteer: () => new Promise(resolve => pending.push(resolve)),
    }))
  }
  const host = await mount(t, h(Host))
  const pasteFile = async name => {
    const file = new window.File(['abc'], name, { type: 'text/plain' })
    const paste = new window.Event('paste', { bubbles: true })
    Object.defineProperty(paste, 'clipboardData', { value: { items: [{ kind: 'file', getAsFile: () => file }] } })
    await act(async () => window.dispatchEvent(paste))
  }
  await pasteFile('first.txt')
  await act(async () => host.querySelector('button[aria-label="Send while running"]').click())
  await type(host.querySelector('textarea'), 'new draft')
  await pasteFile('second.txt')
  await act(async () => pending[0]({ ok: true, state: 'accepted' }))
  assert.equal(host.querySelector('textarea').value, 'new draft')
  assert.match(host.textContent, /first.txt/)
  assert.match(host.textContent, /second.txt/)
  await act(async () => host.querySelector('button[aria-label="Send while running"]').click())
  await act(async () => pending[1]({ ok: true, state: 'accepted' }))
  assert.equal(host.querySelector('textarea').value, '')
  assert.doesNotMatch(host.textContent, /first.txt|second.txt/)
})

test('failed ordinary send restores its unchanged text and uploaded attachment', async t => {
  t.mock.method(dashboardApi, 'uploadSessionAttachments', async () => ({
    attachments: [{ fileId: 'file-ordinary', name: 'brief.txt', mediaType: 'text/plain', size: 3 }],
  }))
  function Host() {
    const [value, setValue] = useState('ordinary draft')
    return h(SessionComposer, props({
      value, onValueChange: setValue, runtimeState: { status: 'idle', selections: {}, metadata: {} },
      effectiveCapabilities: capability('session.send_message', 'runtime.attachment'),
      onSend: async () => false,
    }))
  }
  const host = await mount(t, h(Host))
  const file = new window.File(['abc'], 'brief.txt', { type: 'text/plain' })
  const paste = new window.Event('paste', { bubbles: true })
  Object.defineProperty(paste, 'clipboardData', { value: { items: [{ kind: 'file', getAsFile: () => file }] } })
  await act(async () => window.dispatchEvent(paste))
  await act(async () => host.querySelector('button[aria-label="Send"]').click())
  assert.equal(host.querySelector('textarea').value, 'ordinary draft')
  assert.match(host.textContent, /brief.txt/)
})

test.after(() => dom.window.close())
