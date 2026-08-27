import assert from 'node:assert/strict'
import test from 'node:test'

import type { AdminOverview, RecentFailedRun } from '../src/types/api.ts'
import {
  completePendingCount,
  dashboardOverviewState,
  executorDecisionLabel,
} from '../src/pages/dashboardOverview.ts'

const emptyOverview: AdminOverview = {
  queues: {
    status: 'ok',
    bindings: [{
      stream: 'engineering:queue', group: 'capability-workers', description: 'engineering',
      stream_info: { length: 0 },
      group_info: { consumers: 0, pending: 0, last_delivered_id: '0-0' },
    }],
    issues: [],
  },
  task_counts: {
    backlog: 0, todo: 0, in_dev: 0, in_ci: 0, testing: 0, done: 0, blocked: 0,
    waiting_human_review: 0, waiting_resources: 0, failed: 0, cancelled: 0,
  },
  paid_runs: { queued: 0, running: 0, by_executor: {}, unavailable_executor_decisions: 0 },
  recent_failed_runs: [],
}

test('Dashboard keeps loading, request failure, empty, degraded and healthy states distinct', () => {
  assert.equal(dashboardOverviewState(undefined, true, false), 'loading')
  assert.equal(dashboardOverviewState(undefined, false, true), 'request_failed')
  assert.equal(dashboardOverviewState(emptyOverview, false, false), 'empty')
  assert.equal(dashboardOverviewState({ ...emptyOverview, queues: { ...emptyOverview.queues, status: 'degraded', bindings: [{ stream: 'q', group: 'g', description: 'queue', stream_info: null, group_info: null }], issues: ['Redis error'] } }, false, false), 'degraded')
  assert.equal(dashboardOverviewState({ ...emptyOverview, queues: { ...emptyOverview.queues, bindings: [{ stream: 'q', group: 'g', description: 'queue', stream_info: { length: 2 }, group_info: { consumers: 1, pending: 3, last_delivered_id: '1-0' } }] } }, false, false), 'healthy')
})

test('Dashboard never totals a partial queue snapshot as zero', () => {
  assert.equal(completePendingCount({ ...emptyOverview, queues: { ...emptyOverview.queues, bindings: [{ stream: 'q', group: 'g', description: 'queue', stream_info: null, group_info: null }] } }), null)
})

test('Dashboard labels legacy and invalid decisions without guessing', () => {
  const run = { id: 'r', type: 'engineering', project_id: null, task_id: null, story_id: null, error_message: 'error', created_at: '2026-01-01T00:00:00Z', started_at: null, completed_at: null, executor_decision: null } as Omit<RecentFailedRun, 'executor_decision_availability'>
  assert.equal(executorDecisionLabel({ ...run, executor_decision_availability: 'legacy' }), 'Unavailable for legacy Run')
  assert.equal(executorDecisionLabel({ ...run, executor_decision_availability: 'invalid' }), 'Unavailable: invalid persisted snapshot')
})
