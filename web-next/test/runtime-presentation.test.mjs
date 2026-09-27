import assert from 'node:assert/strict'
import test from 'node:test'
import { registerSource } from './helpers/onboarding-source.mjs'
const hook = registerSource()
const { presentationSemantics, readGoal, latestPlanItems, runtimeStatesSemanticallyEqual } = await import('../src/components/session/runtime-presentation.ts')
hook.deregister()

const state = metadata => ({sessionId:'s',runtime:'codex',status:'idle',selections:{},metadata})
const goal = {objective:'目标',status:'active',tokensUsed:0,timeUsedSeconds:0,tokenBudget:null}

test('goal presence, explicit null and diagnostics participate correctly in equality', () => {
  const unknown = presentationSemantics(state({diagnostic:1}))
  assert.deepEqual(unknown, presentationSemantics(state({diagnostic:2})))
  assert.notDeepEqual(unknown, presentationSemantics(state({codexPresentation:{threadGoal:null}})))
  assert.notDeepEqual(presentationSemantics(state({codexPresentation:{threadGoal:goal}})), presentationSemantics(state({codexPresentation:{threadGoal:{...goal,tokensUsed:1}}})))
  assert.equal(runtimeStatesSemanticallyEqual(state({codexPresentation:{threadGoal:goal},diagnostic:1}),state({codexPresentation:{threadGoal:goal},diagnostic:2})),true)
  assert.equal(runtimeStatesSemanticallyEqual(state({codexPresentation:{threadGoal:goal}}),state({codexPresentation:{threadGoal:null}})),false)
  assert.equal(runtimeStatesSemanticallyEqual(state({codexPresentation:{threadGoal:goal}}),state({codexPresentation:{threadGoal:{...goal,tokensUsed:1}}})),false)
  assert.equal(runtimeStatesSemanticallyEqual(state({codexCoordination:{available:true}}),state({codexCoordination:{available:false}})),false)
  assert.equal(runtimeStatesSemanticallyEqual(state({codexSettings:{latestThreadSettings:{collaborationMode:{mode:'default'}}}}),state({codexSettings:{latestThreadSettings:{collaborationMode:{mode:'plan'}}}})),false)
  assert.equal(readGoal(state({codexPresentation:{threadGoal:goal,completedThreadGoal:{...goal,status:'complete'}}}))?.status,'active')
  assert.equal(readGoal(state({codexPresentation:{threadGoal:{...goal,status:'complete'},completedThreadGoal:{...goal,status:'complete'}}}))?.status,'complete')
})

test('latest plan per physical turn, stable display id, distinct Markdown items', () => {
  const plan = (id,turn,seq,step) => ({id,type:'artifact',updatedSeq:seq,orderSeq:seq,content:{kind:'plan-progress',plan:[{step,status:'pending'}]},source:{turnId:turn}})
  const older = plan('plan:t1','t1',1,'old')
  const newer = plan('plan:t1','t1',2,'new')
  const markdown = {id:'md',type:'artifact',content:{kind:'plan',text:'# Plan'},source:{turnId:'t1'}}
  assert.deepEqual(latestPlanItems([older,newer,markdown,plan('plan:t2','t2',3,'two')]).map(item => item.id),['plan:t1','md','plan:t2'])
  assert.equal(latestPlanItems([older,newer])[0].content.plan[0].step,'new')
})
