import assert from 'node:assert/strict'
import test from 'node:test'

import { planningRetryTarget, requestPlanningRetry } from '../src/pages/storyPlanningRetry.ts'
import type { Story } from '../src/types/api.ts'

const parked = {
  id: 'story-92b433c8',
  status: 'waiting_human_review',
  quarantine_reason: {
    reason: 'story_failure',
    code: 'planning_failed',
    source: 'architect',
    detail: 'LLMChannelsExhausted: every LLM channel failed: openrouter:payment_required',
  },
  planning: { state: 'parked', failed_attempts: 1, recorded_at: '2026-09-26T00:00:00Z' },
} as Story

test('a story parked by a planning failure exposes the planning retry', () => {
  assert.deepEqual(planningRetryTarget(parked), {
    detail: 'LLMChannelsExhausted: every LLM channel failed: openrouter:payment_required',
    failedAttempts: 1,
  })
})

test('any other human-review stop offers no planning retry', () => {
  const scaffold = {
    ...parked,
    quarantine_reason: { ...parked.quarantine_reason, code: 'scaffold_timeout' },
  } as Story
  assert.equal(planningRetryTarget(scaffold), null)
  assert.equal(planningRetryTarget({ ...parked, status: 'in_progress' } as Story), null)
})

test('the planning retry posts to the API action', async () => {
  const calls: Array<{ path: string, body: unknown }> = []
  const api = {
    post: async <T>(path: string, body: unknown): Promise<T> => {
      calls.push({ path, body })
      return {} as T
    },
  }

  await requestPlanningRetry(api, 'story-92b433c8')

  assert.deepEqual(calls, [{
    path: '/stories/story-92b433c8/retry-planning',
    body: { actor: 'admin' },
  }])
})
