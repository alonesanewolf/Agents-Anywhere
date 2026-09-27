import assert from 'node:assert/strict'
import test from 'node:test'
import { registerSource } from './helpers/onboarding-source.mjs'

const hook = registerSource()
const { parseSlashIntent, commandRequest, commandAllowed, commandResult, commandActionReason, commandTransportFailure } = await import('../src/components/session/runtime-command-model.ts')
const { ApiError } = await import('../src/lib/api/errors.ts')
hook.deregister()

const goal = { id: 'goal', aliases: [], enabled: true, disabledReason: null, acceptsArgs: true, argsSchema: {type:'string'}, metadata: { ui: {kind:'execute', acceptsMultiline:true, allowedStatuses:['idle','running']} } }

test('exact multiline raw and long free-form input remain one argument', () => {
  const raw = ` /goal create ${Array.from({length:40}, (_, i) => `word${i}`).join(' ')}\nsecond line  `
  const intent = parseSlashIntent(raw)
  assert.equal(intent?.command, 'goal')
  assert.equal(intent?.multiline, true)
  assert.deepEqual(commandRequest(intent, goal), { command:'goal', args:[raw.slice(raw.indexOf('create'))], raw })
})

test('unknown, unsupported multiline, busy and unavailable are not executable', () => {
  assert.equal(parseSlashIntent(' /missing hello')?.command, 'missing')
  assert.equal(commandAllowed(goal, 'running', true, true, true), true)
  assert.equal(commandAllowed(goal, 'blocked', true, true, true), false)
  assert.equal(commandAllowed(goal, 'idle', false, true, true), false)
  assert.equal(commandAllowed(goal, 'idle', true, false, true), false)
  assert.equal(commandAllowed(goal, 'idle', true, true, false), false)
  assert.equal(commandRequest(parseSlashIntent('/compact\nfoo'), {...goal, id:'compact', acceptsArgs:false, metadata:{ui:{kind:'execute',acceptsMultiline:false}}}), null)
})

test('outer failure overrides nested acknowledgement; unknown is never success', () => {
  assert.equal(commandResult({ok:false,code:'goal_pause_unknown',message:'uncertain',result:{ok:true,executionState:'unknown'}}).ok, false)
  assert.equal(commandResult({ok:false,code:'invalid_args',message:'Invalid arguments',result:{}}).state, 'completed')
  assert.equal(commandResult({ok:true,message:'queued',result:{executionState:'accepted'}}).state, 'accepted')
  assert.equal(commandResult({ok:true,message:'done',result:{executionState:'unknown'}}).ok, false)
})

test('pre-dispatch HTTP validation is a known rejection while network loss is unknown',()=>{
  assert.equal(commandTransportFailure(new ApiError({status:409,kind:'http',detail:'Capability unavailable',code:'command_unavailable'}),'fallback').state,'completed')
  assert.equal(commandTransportFailure(new ApiError({status:0,kind:'network',detail:'Connection lost'}),'fallback').state,'unknown')
})

test('goal mutation reasons are specific to the native goal action',()=>{
  const item={...goal,metadata:{...goal.metadata,goalActions:{pause:{enabled:false,disabledReason:'goal_requires_aa_owner'},create:{enabled:true,disabledReason:null}}}}
  assert.equal(commandActionReason(parseSlashIntent('/goal pause'),item),'goal_requires_aa_owner')
  assert.equal(commandActionReason(parseSlashIntent('/goal create work'),item),null)
  assert.equal(commandActionReason(parseSlashIntent('/goal status'),item),null)
})
