import assert from 'node:assert/strict'
import test from 'node:test'
import { registerSource } from './helpers/onboarding-source.mjs'
const hook=registerSource()
const {sessionStopOutcome}=await import('../src/components/session/session-stop-result.ts')
hook.deregister()
const t=(key,values)=>key==='stopPartialTurn'?`Physical ${values.turn} confirmed; goal unconfirmed`:key

test('outer stop failure outranks nested native acknowledgement and exposes partial physical turn',()=>{
  const outcome=sessionStopOutcome({ok:false,result:{ok:true,interruptedTurnId:'physical',goalPaused:false,goalStopped:false,goalStatus:'active',executionState:'unknown',retryable:false}},t)
  assert.deepEqual(outcome,{ok:false,message:'stopUnknownOutcome Physical physical confirmed; goal unconfirmed'})
})

test('authoritative goalStopped true with a newer clear/completion is accepted without falsely requiring goalPaused',()=>{
  for (const status of [null,'complete']) assert.deepEqual(sessionStopOutcome({ok:true,result:{ok:true,goalPaused:false,goalStopped:true,goalStatus:status,executionState:'accepted'}},t),{ok:true,message:'stopAccepted'})
})
