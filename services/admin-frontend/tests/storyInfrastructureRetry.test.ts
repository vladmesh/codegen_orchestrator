import assert from 'node:assert/strict'
import test from 'node:test'

import {
  infrastructureRetryTarget,
  requestInfrastructureRetry,
} from '../src/pages/storyInfrastructureRetry.ts'
import type { Story, Task } from '../src/types/api.ts'

const evidence = {
  execution_phase: 'pre_agent_refused',
  refusal: 'project_locked',
  task_id: 'task-1',
  attempt_id: 'eng-1',
  detail: 'Release the stale project worker, then retry.',
}
const story = {
  id: 'story-1',
  status: 'waiting_human_review',
  quarantine_reason: { engineering_infrastructure: evidence },
} as Story
const task = {
  id: 'task-1',
  status: 'waiting_human_review',
  failure_metadata: { engineering_infrastructure: evidence },
} as Task

test('typed matching infrastructure evidence exposes one retry target', () => {
  assert.deepEqual(infrastructureRetryTarget(story, [task]), {
    taskId: 'task-1',
    attemptId: 'eng-1',
    refusal: 'project_locked',
    detail: 'Release the stale project worker, then retry.',
  })
})

test('a failed workspace ensure park exposes the same retry target', () => {
  const workspace = { ...evidence, refusal: 'workspace_ensure_failed', attempt_id: 'ws-1' }
  assert.equal(infrastructureRetryTarget(
    { ...story, quarantine_reason: { engineering_infrastructure: workspace } },
    [{ ...task, failure_metadata: { engineering_infrastructure: workspace } }],
  )?.refusal, 'workspace_ensure_failed')
})

test('QA, product, budget, and mismatched task parks expose no retry target', () => {
  assert.equal(infrastructureRetryTarget({ ...story, quarantine_reason: { blocker: {} } }, [task]), null)
  assert.equal(infrastructureRetryTarget({ ...story, quarantine_reason: { reason: 'product' } }, [task]), null)
  assert.equal(infrastructureRetryTarget({
    ...story,
    quarantine_reason: {
      engineering_infrastructure: { ...evidence, refusal: 'engineering_budget_denied' },
    },
  }, [task]), null)
  assert.equal(infrastructureRetryTarget(story, [{ ...task, failure_metadata: null }]), null)
})

test('one click invokes the composite endpoint with exact evidence', async () => {
  const calls: Array<{ path: string, body: unknown }> = []
  const api = {
    post: async <T>(path: string, body: unknown): Promise<T> => {
      calls.push({ path, body })
      return {} as T
    },
  }
  const target = infrastructureRetryTarget(story, [task])!

  await requestInfrastructureRetry(api, story.id, target)

  assert.deepEqual(calls, [{
    path: '/stories/story-1/retry-infrastructure-attempt',
    body: {
      task_id: 'task-1',
      attempt_id: 'eng-1',
      refusal: 'project_locked',
      actor: 'admin',
    },
  }])
})
