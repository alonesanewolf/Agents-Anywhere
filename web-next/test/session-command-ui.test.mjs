import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { JSDOM } from 'jsdom'
import { registerSource } from './helpers/onboarding-source.mjs'

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url:'https://app.example.test/', pretendToBeVisual:true })
for (const key of ['window','document','navigator','HTMLElement','HTMLTextAreaElement','HTMLFormElement','Element','Node','MutationObserver','getComputedStyle','requestAnimationFrame','cancelAnimationFrame']) {
  Object.defineProperty(globalThis,key,{configurable:true,value:dom.window[key]})
}
globalThis.ResizeObserver = class {observe(){} disconnect(){}}
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const hook = registerSource()
const { createElement:h, act, useState } = await import('react')
const { createRoot } = await import('react-dom/client')
const { NextIntlClientProvider } = await import('next-intl')
const { SessionComposer } = await import('../src/components/session/session-composer.tsx')
const { SessionGoalPanel } = await import('../src/components/session/session-goal-panel.tsx')
const { SessionPlanCard } = await import('../src/components/session/session-plan-card.tsx')
const { InteractionCard } = await import('../src/components/session/session-approval-card.tsx')
hook.deregister()
const messages = JSON.parse(readFileSync(new URL('../messages/en.json',import.meta.url),'utf8'))
const session = {id:'s1',runtime:'codex',runtimeId:'codex',connectorStatus:'online',status:'idle',archived:false,takeover:true}
const capability = {revision:1,capabilities:[{capabilityId:'session.commands',scope:'session',runtime:'codex',runtimeId:'codex',sessionId:'s1',supported:true,available:true,allowed:true},{capabilityId:'session.interrupt',scope:'session',runtime:'codex',runtimeId:'codex',sessionId:'s1',supported:true,available:true,allowed:true}]}
const descriptor = (id, acceptsArgs, statuses=['idle']) => ({id,title:id,description:'native',aliases:[],scope:'session',enabled:true,disabledReason:null,acceptsArgs,argsSchema:{type:'string'},metadata:{ui:{kind:'execute',acceptsMultiline:true,allowedStatuses:statuses}}})

async function mount(t, child) {
  const host = document.createElement('div'); document.body.append(host)
  const root = createRoot(host)
  await act(async () => root.render(h(NextIntlClientProvider,{locale:'en',messages,timeZone:'UTC'},child)))
  t.after(async () => {await act(async () => root.unmount());host.remove()})
  return host
}
const textSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set
async function type(input,value) {await act(async()=>{textSetter.call(input,value);input.dispatchEvent(new window.Event('input',{bubbles:true}))})}

test('composer inserts argument command, submits exact long raw once, preserves new typing after delayed accepted result', async t => {
  let resolve
  const calls=[];let messagesSent=0
  function Host() {
    const [value,setValue] = useState('/')
    return h(SessionComposer,{token:'test',session,runtimeState:{status:'idle',metadata:{},selections:{}},pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value,effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[descriptor('goal',true)],onCommandQueryChange(){},onValueChange:setValue,onSelectionChange:async()=>true,onSend:async()=>{messagesSent++;return true},onInterrupt(){},onToggleTakeover(){},onCommand:(id,payload)=>{calls.push({id,payload});return new Promise(done=>resolve=done)}})
  }
  const host=await mount(t,h(Host))
  const menu=[...host.querySelectorAll('button')].find(button=>button.textContent.includes('/goal'))
  assert.ok(menu)
  await act(async()=>menu.click())
  const input=host.querySelector('textarea')
  assert.equal(input.value,'/goal ')
  assert.equal(document.activeElement,input)
  assert.equal(calls.length,0)
  const raw=` /goal create ${Array.from({length:40},(_,i)=>`word${i}`).join(' ')}\nsecond line  `
  await type(input,raw)
  await act(async()=>input.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  assert.equal(calls.length,1)
  assert.equal(calls[0].payload.raw,raw)
  assert.equal(calls[0].payload.args.length,1)
  await act(async()=>input.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  assert.equal(calls.length,1)
  await type(input,'/goal status')
  await act(async()=>resolve({ok:true,state:'accepted',message:'Queued',code:null,result:{executionState:'accepted'}}))
  assert.equal(input.value,'/goal status')
  assert.match(host.textContent,/Queued/)
  assert.equal(messagesSent,0)
})

test('an old command acknowledgement cannot clear or report against the newly selected session',async t=>{
  let resolve
  function Host(){const [selected,setSelected]=useState(session);const [value,setValue]=useState('/compact');window.switchCommandSession=()=>{setSelected({...session,id:'s2'});setValue('new session draft')};return h(SessionComposer,{token:'test',session:selected,runtimeState:{status:'idle',metadata:{},selections:{}},pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value,effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[descriptor('compact',false)],onCommandQueryChange(){},onValueChange:setValue,onSelectionChange:async()=>true,onSend:async()=>true,onInterrupt(){},onToggleTakeover(){},onCommand:async()=>new Promise(done=>resolve=done)})}
  const host=await mount(t,h(Host));const input=host.querySelector('textarea')
  await act(async()=>input.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  await act(async()=>window.switchCommandSession())
  await act(async()=>resolve({ok:true,state:'accepted',code:null,message:'old session accepted',result:{executionState:'accepted'}}))
  assert.equal(input.value,'new session draft')
  assert.doesNotMatch(host.textContent,/old session accepted/)
})

test('unknown slash and unsupported multiline never become a model prompt; native ok:false retains the draft',async t=>{
  let sent=0;let calls=0
  function Host(){const [value,setValue]=useState('/missing what');return h(SessionComposer,{token:'test',session,runtimeState:{status:'idle',metadata:{},selections:{}},pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value,effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[descriptor('compact',false)],onCommandQueryChange(){},onValueChange:setValue,onSelectionChange:async()=>true,onSend:async()=>{sent++;return true},onInterrupt(){},onToggleTakeover(){},onCommand:async()=>{calls++;return {ok:false,state:'completed',code:'command_error',message:'native rejected',result:{executionState:'completed'}}}})}
  const host=await mount(t,h(Host));const input=host.querySelector('textarea')
  await act(async()=>input.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  assert.match(host.querySelector('[role=alert]').textContent,/Unknown command/)
  await type(input,'/compact\ninvalid')
  await act(async()=>input.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  assert.match(host.querySelector('[role=alert]').textContent,/does not accept multiple lines/)
  assert.equal(input.value,'/compact\ninvalid')
  await type(input,'/compact')
  await act(async()=>input.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  assert.equal(calls,1)
  assert.equal(input.value,'/compact')
  assert.match(host.textContent,/native rejected/)
  assert.equal(sent,0)
})

test('read-only slash command remains unavailable while draft is intact',async t=>{
  let executed=0
  function Host(){const [value,setValue]=useState('/compact');return h(SessionComposer,{token:'test',session:{...session,takeover:false},runtimeState:{status:'idle',metadata:{},selections:{}},pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value,effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[descriptor('compact',false)],onCommandQueryChange(){},onValueChange:setValue,onSelectionChange:async()=>true,onSend:async()=>true,onInterrupt(){},onToggleTakeover(){},onCommand:async()=>{executed++;return {ok:true,state:'accepted'}}})}
  const host=await mount(t,h(Host));const input=host.querySelector('textarea')
  await act(async()=>input.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  assert.equal(executed,0)
  assert.equal(input.value,'/compact')
  assert.match(host.querySelector('[role=alert]').textContent,/unavailable/)
})

test('selector command with no available native settings control reports unavailable without dispatch',async t=>{
  let executed=0
  const model={...descriptor('model',false),metadata:{ui:{kind:'selector',target:'model'}}}
  const host=await mount(t,h(SessionComposer,{token:'test',session,runtimeState:{status:'idle',metadata:{},selections:{}},pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value:'/model',effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[model],onCommandQueryChange(){},onValueChange(){},onSelectionChange:async()=>true,onSend:async()=>true,onInterrupt(){},onToggleTakeover(){},onCommand:async()=>{executed++;return {ok:true,state:'accepted'}}}))
  await act(async()=>host.querySelector('textarea').dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  assert.match(host.querySelector('[role=alert]').textContent,/unavailable/)
  assert.equal(executed,0)
})

test('goal displays zero, no fake budget, and only one completed card with current and completed goal', async t => {
  const goal={objective:'目标：完成任务',status:'complete',tokensUsed:0,timeUsedSeconds:0,tokenBudget:null}
  const host=await mount(t,h(SessionGoalPanel,{state:{metadata:{codexPresentation:{threadGoal:goal,completedThreadGoal:goal}}}}))
  assert.equal(host.textContent.match(/目标：完成任务/g)?.length,1)
  assert.equal(host.querySelectorAll('[data-slot="card"]').length,1)
  const button=host.querySelector('button')
  await act(async()=>button.click())
  assert.match(host.textContent,/0 tokens used/)
  assert.doesNotMatch(host.textContent,/token budget/)
})

test('goal panel responds to authoritative null clear but retains presentation on authority loss',async t=>{
  const goal={objective:'Retained native goal',status:'active',tokensUsed:0,timeUsedSeconds:0}
  function Host(){const [metadata,setMetadata]=useState({codexPresentation:{threadGoal:goal},codexCoordination:{available:true}});window.loseGoalAuthority=()=>setMetadata({codexPresentation:{threadGoal:goal},codexCoordination:{available:false}});window.clearGoal=()=>setMetadata({codexPresentation:{threadGoal:null,completedThreadGoal:null},codexCoordination:{available:false}});return h(SessionGoalPanel,{state:{metadata}})}
  const host=await mount(t,h(Host))
  assert.match(host.textContent,/Retained native goal/)
  await act(async()=>window.loseGoalAuthority())
  assert.match(host.textContent,/Retained native goal/)
  await act(async()=>window.clearGoal())
  assert.doesNotMatch(host.textContent,/Retained native goal/)
})

test('plan updates retain the mounted card while replacing old steps', async t => {
  const plan = step => ({id:'p',sessionId:'s1',type:'artifact',content:{kind:'plan-progress',plan:[{step,status:'pending'}]},source:{turnId:'t1'}})
  function Host() { const [item,setItem]=useState(plan('old'));window.updatePlan=()=>setItem(plan('new'));return h(SessionPlanCard,{item,session,token:'test'}) }
  const host=await mount(t,h(Host))
  const card=host.querySelector('[data-slot="card"]')
  assert.match(host.textContent,/old/)
  await act(async()=>window.updatePlan())
  assert.equal(host.querySelector('[data-slot="card"]'),card)
  assert.doesNotMatch(host.textContent,/old/)
  assert.match(host.textContent,/new/)
})

test('native plan review renders Markdown and dispatches exact selected option once', async t => {
  const calls=[]
  const notice={noticeId:'opaque-authority',type:'interaction',interactionType:'input_request',sessionId:'s1',source:{runtime:'dsh',component:'dsh.plan_review'},title:'Review plan',severity:'info',status:'open',responseRequired:true,context:{},metadata:{},actions:[{actionId:'submit',label:'Submit',style:'primary',input:{required:true,uiSchema:{component:'inputRequest',version:1,questions:[{id:'q1',prompt:'Review this?\n\n# Native plan\n\n- Check output',intent:{kind:'plan-review',approveOptionId:'o_0'},options:[{id:'o_0',label:'Approve'},{id:'o_1',label:'Revise'}]}]}}}]}
  const host=await mount(t,h(InteractionCard,{notice,resolvingNoticeId:null,resolvingActionId:null,onRespondInteraction:(...args)=>calls.push(args)}))
  assert.ok(host.querySelector('h1'))
  assert.match(host.textContent,/Check output/)
  const option=host.querySelector('[role=radio]')
  await act(async()=>option.click())
  const submit=[...host.querySelectorAll('button')].find(button=>button.textContent.includes('Submit'))
  await act(async()=>submit.click())
  assert.deepEqual(calls,[['opaque-authority','submit',{answers:{q1:{optionIds:['o_0']}}}]])
})

test('native validation failure keeps the same question draft correctable',async t=>{
  const base={noticeId:'opaque-form',type:'interaction',interactionType:'input_request',sessionId:'s1',source:{runtime:'codex'},title:'Input',severity:'info',status:'open',responseRequired:true,context:{},metadata:{},actions:[{actionId:'submit',label:'Submit',style:'primary',input:{required:true,uiSchema:{component:'inputRequest',version:1,questions:[{id:'q',prompt:'Answer?',options:[{id:'o1',label:'A'},{id:'o2',label:'B'}]}]}}}]}
  const calls=[]
  function Host(){const [notice,setNotice]=useState(base);window.validationFailed=()=>setNotice({...base,metadata:{responseOutcome:'validation_failed',retryable:true,error:'Please try again'}});return h(InteractionCard,{notice,resolvingNoticeId:null,resolvingActionId:null,onRespondInteraction:(...args)=>calls.push(args)})}
  const host=await mount(t,h(Host))
  await act(async()=>host.querySelectorAll('[role=radio]')[1].click())
  await act(async()=>window.validationFailed())
  assert.equal(host.querySelectorAll('[role=radio]')[1].getAttribute('aria-checked'),'true')
  await act(async()=>[...host.querySelectorAll('button')].find(button=>button.textContent.includes('Submit')).click())
  assert.deepEqual(calls,[['opaque-form','submit',{answers:{q:{optionIds:['o2']}}}]])
})

test('public/read-only interaction suppresses actionable native buttons',async t=>{
  const notice={noticeId:'n',type:'interaction',interactionType:'input_request',sessionId:'s1',source:{},title:'Approve?',severity:'info',status:'open',responseRequired:true,context:{},metadata:{},actions:[{actionId:'approve',label:'Approve',style:'primary',input:{required:true,uiSchema:{component:'inputRequest',version:1,questions:[{id:'q1',prompt:'Read this plan\n\n# Plan details',intent:{kind:'plan-review',approveOptionId:'yes'},options:[{id:'yes',label:'Approve'}]}]}}}]}
  const host=await mount(t,h(InteractionCard,{notice,readOnly:true,resolvingNoticeId:null,resolvingActionId:null,onRespondInteraction:()=>assert.fail('readonly')}))
  assert.equal(host.querySelectorAll('button').length,0)
  assert.equal(host.querySelectorAll('input,[role=radio],[role=checkbox]').length,0)
  assert.match(host.textContent,/Read this plan/)
  assert.ok(host.querySelector('h1'))
})

test('unknown interaction offers no retry, URL elicitation keeps safe link and read-only hides actions', async t => {
  const notice={noticeId:'opaque-new',type:'interaction',sessionId:'s1',source:{runtime:'codex'},title:'Authorize',severity:'warning',status:'unknown',responseRequired:false,context:{nativeRequest:{params:{mode:'url',url:'https://example.test/authorize'}}},metadata:{responseOutcome:'unknown',retryable:false},actions:[]}
  const host=await mount(t,h(InteractionCard,{notice,resolvingNoticeId:null,resolvingActionId:null,onRespondInteraction:()=>assert.fail('No response')}))
  assert.match(host.querySelector('[role=alert]').textContent,/outcome is unknown/)
  assert.equal(host.querySelector('a[href="https://example.test/authorize"]')?.getAttribute('target'),'_blank')
  assert.equal(host.querySelectorAll('button').length,0)
})

test('between-turn observed active goal exposes Stop only with native route capability',async t=>{
  let stopped=0
  const state={status:'idle',selections:{},metadata:{codexPresentation:{threadGoal:{objective:'work',status:'active'}},codexCapabilities:{userSessionStop:true}}}
  const host=await mount(t,h(SessionComposer,{token:'test',session,runtimeState:state,pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value:'',effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[],onCommandQueryChange(){},onValueChange(){},onSelectionChange:async()=>true,onSend:async()=>true,onInterrupt(){stopped++},onToggleTakeover(){},onCommand:async()=>({ok:true,state:'accepted'})}))
  const stop=host.querySelector('button[aria-label="Interrupt"]')
  assert.ok(stop)
  await act(async()=>stop.click())
  assert.equal(stopped,1)
})

test('a retained goal after authority loss cannot expose a stale between-turn Stop',async t=>{
  const state={status:'idle',selections:{},metadata:{codexPresentation:{threadGoal:{objective:'work',status:'active'}},codexCoordination:{available:false},codexCapabilities:{userSessionStop:true}}}
  const host=await mount(t,h(SessionComposer,{token:'test',session,runtimeState:state,pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value:'',effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[],onCommandQueryChange(){},onValueChange(){},onSelectionChange:async()=>true,onSend:async()=>true,onInterrupt(){assert.fail('stale stop')},onToggleTakeover(){},onCommand:async()=>({ok:true,state:'accepted'})}))
  assert.equal(host.querySelector('button[aria-label="Interrupt"]'),null)
})

test('a slash command in a running turn presents Send and dispatches the command rather than Stop',async t=>{
  let stopped=0;const calls=[]
  function Host(){const [value,setValue]=useState('/goal status');return h(SessionComposer,{token:'test',session,runtimeState:{status:'running',metadata:{},selections:{}},pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value,effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[descriptor('goal',true,['running'])],onCommandQueryChange(){},onValueChange:setValue,onSelectionChange:async()=>true,onSend:async()=>true,onInterrupt(){stopped++},onToggleTakeover(){},onCommand:async(id,payload)=>{calls.push({id,payload});return {ok:true,state:'accepted',message:'accepted'}}})}
  const host=await mount(t,h(Host));const button=host.querySelector('button[aria-label="Send"]')
  assert.ok(button)
  await act(async()=>button.click())
  assert.equal(stopped,0)
  assert.deepEqual(calls.map(({id,payload})=>({id,raw:payload.raw})),[{id:'goal',raw:'/goal status'}])
})

test('a partial unknown Stop stays visibly unresolved without a retry action',async t=>{
  const host=await mount(t,h(SessionComposer,{token:'test',session,runtimeState:{status:'idle',metadata:{},selections:{}},pendingInteractionCount:0,sending:false,interrupting:false,takeoverBusy:false,value:'',effectiveCapabilities:capability,modelCatalog:null,permissionCatalog:null,runtimeCommands:[],stopOutcome:{ok:false,message:'Turn physical interrupted; goal pause unknown'},onCommandQueryChange(){},onValueChange(){},onSelectionChange:async()=>true,onSend:async()=>true,onInterrupt(){},onToggleTakeover(){},onCommand:async()=>({ok:true,state:'accepted'})}))
  assert.match(host.querySelector('[role=alert]').textContent,/goal pause unknown/)
  assert.equal(host.querySelectorAll('button[aria-label="Retry"]').length,0)
})

test.after(()=>dom.window.close())
