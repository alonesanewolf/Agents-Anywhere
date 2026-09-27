import assert from 'node:assert/strict'
import test from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { JSDOM } from 'jsdom'
import { registerSource } from './helpers/onboarding-source.mjs'
const dom=new JSDOM('<!doctype html><html><body></body></html>',{pretendToBeVisual:true})
for(const key of ['window','document','navigator','HTMLElement','Element','Node'])Object.defineProperty(globalThis,key,{configurable:true,value:dom.window[key]})
globalThis.IS_REACT_ACT_ENVIRONMENT=true
const hook=registerSource()
const {createElement:h,act}=await import('react')
const {createRoot}=await import('react-dom/client')
const {dashboardApi}=await import('../src/features/dashboard/api.ts')
const {useRuntimeCommands}=await import('../src/components/session/use-runtime-commands.ts')
hook.deregister()

test('catalog invalidation, unavailable, reopen, stale response and fetch error are separate states',async t=>{
  const deferred=[]
  t.mock.method(dashboardApi,'getSessionCommands',async(_token,id)=>new Promise((resolve,reject)=>deferred.push({id,resolve,reject})))
  const host=document.createElement('div');document.body.append(host)
  const root=createRoot(host)
  const Host=props=>{const state=useRuntimeCommands({token:'token',...props});window.catalogState=state;return h('output',null,`${state.loading?'loading':state.error?'error':'ready'}:${state.commands.map(x=>x.id).join(',')}`)}
  const render=async props=>act(async()=>root.render(h(Host,props)))
  t.after(async()=>{await act(async()=>root.unmount());host.remove()})
  await render({sessionId:'a',open:true,available:true,catalogRevision:'1'})
  await act(async()=>delay(140))
  assert.equal(deferred.length,1)
  await render({sessionId:'b',open:true,available:true,catalogRevision:'1'})
  await act(async()=>deferred[0].resolve({commands:[{id:'stale'}]}))
  assert.doesNotMatch(host.textContent,/stale/)
  await act(async()=>delay(140))
  await act(async()=>deferred[1].resolve({commands:[{id:'live'}]}))
  assert.match(host.textContent,/ready:live/)
  await render({sessionId:'b',open:true,available:true,catalogRevision:'2'})
  await act(async()=>delay(140))
  await act(async()=>deferred[2].reject(new Error('offline')))
  assert.match(host.textContent,/error:/)
  await render({sessionId:'b',open:true,available:false,catalogRevision:'2'})
  assert.match(host.textContent,/ready:/)
  assert.equal(deferred.length,3)
  await render({sessionId:'b',open:false,available:true,catalogRevision:'2'})
  await render({sessionId:'b',open:true,available:true,catalogRevision:'2'})
  await act(async()=>delay(140))
  assert.equal(deferred.length,4)
})
test.after(()=>dom.window.close())
