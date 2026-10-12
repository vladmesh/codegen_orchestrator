import assert from 'node:assert/strict'
import test from 'node:test'

import type { JourneyStep, Task } from '../src/types/api.ts'
import { boardColumns, cardTitle, columnOf, isFixTask, prCard } from '../src/pages/taskBoard.ts'

function task(id: string, status: string, priority: number, extra: Partial<Task> = {}): Task {
  return {
    id, project_id: 'p', type: 'feature', title: `Task ${id}`, description: null, status, priority,
    acceptance_criteria: null, current_iteration: 1, max_iterations: 5, created_by: 'architect',
    dispatch_admitted: true, created_at: '2026-10-12T10:00:00Z', ...extra,
  }
}

test('boardColumns keeps the four base columns and orders cards by priority', () => {
  const cols = boardColumns([task('b', 'done', 2), task('a', 'done', 1), task('c', 'todo', 3), task('d', 'in_ci', 4)])
  assert.deepEqual(cols.map((c) => c.label), ['Backlog', 'In Dev', 'CI', 'Done'])
  assert.deepEqual(cols[3].tasks.map((t) => t.id), ['a', 'b'])
  assert.deepEqual(cols[0].tasks.map((t) => t.id), ['c'])
  assert.equal(boardColumns([]).length, 4)
})

test('Review and QA columns appear only when a task is there', () => {
  const cols = boardColumns([task('a', 'testing', 1)])
  assert.deepEqual(cols.map((c) => c.id), ['backlog', 'in_dev', 'in_ci', 'qa', 'done'])
  assert.equal(columnOf('mystery'), 'backlog')
})

test('fix tasks are recognised by type or title', () => {
  assert.equal(isFixTask({ type: 'bugfix', title: 'Streak' }), true)
  assert.equal(isFixTask({ type: 'feature', title: 'Fix: midnight' }), true)
  assert.equal(isFixTask({ type: 'feature', title: 'Fixtures loader' }), false)
  assert.equal(cardTitle({ type: 'bugfix', title: "Fix: streak resets at the user's local midnight" }),
    "streak resets at the user's local midnight")
})

test('prCard follows the PR through CI, QA and merge', () => {
  type A = JourneyStep['attempts'][number]
  const run = (status: string, error: string | null = null): A =>
    ({ id: 'x', kind: 'engineering', status, task_id: null, actor: 'GitHub CI', started_at: null, finished_at: null, error })
  const steps = (review: JourneyStep['status'], attempts: A[], verify: JourneyStep['status'] = 'pending'): JourneyStep[] => [
    { stage: 'review', status: review, started_at: null, finished_at: null, attempts, facts: [] },
    { stage: 'verify', status: verify, started_at: null, finished_at: null, attempts: [], facts: [] },
  ]
  assert.equal(prCard(steps('pending', []), null), null)
  assert.equal(prCard(steps('active', [run('running')]), 1)?.status, 'CI run 1 running')
  const failed = prCard(steps('waiting', [run('failed', 'boom')]), 1)
  assert.deepEqual([failed?.column, failed?.tone, failed?.detail], ['in_ci', 'failed', 'boom'])
  assert.equal(prCard(steps('done', [run('failed'), run('completed')]), 1)?.detail, 'CI run 2 passed')
  assert.equal(prCard(steps('done', [run('completed')], 'active'), 1)?.column, 'qa')
  assert.deepEqual(boardColumns([], ['qa']).map((c) => c.id), ['backlog', 'in_dev', 'in_ci', 'qa', 'done'])
})
