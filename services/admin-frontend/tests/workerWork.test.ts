import assert from 'node:assert/strict'
import test from 'node:test'

import type { WorkerSummary } from '../src/types/api.ts'
import {
  lookupOf,
  runTaskIds,
  workerLookupIds,
  workerWorkRow,
  type Lookup,
  type WorkerWorkLookups,
} from '../src/pages/workerWork.ts'

function worker(id: string, overrides: Partial<WorkerSummary> = {}): WorkerSummary {
  return {
    id,
    status: 'RUNNING',
    story_id: null,
    project_id: null,
    repo_id: null,
    workspace_path: null,
    dev_network: null,
    error: null,
    container: null,
    container_error: null,
    agent_process_status: null,
    agent_process_status_error: null,
    active_turn_lease: null,
    active_turn_lease_error: null,
    story_bindings: [],
    story_bindings_error: null,
    attempt_run: null,
    attempt_run_error: null,
    waiting_attempt: null,
    waiting_attempt_error: null,
    ...overrides,
  }
}

const found = <T>(value: T): Lookup<T> => ({ state: 'found', value })

function lookups(overrides: Partial<WorkerWorkLookups> = {}): WorkerWorkLookups {
  return {
    runTasks: new Map([['run-1', found<string | null>('task-1')]]),
    taskTitles: new Map([['task-1', found('Add the streak counter')]]),
    storyTitles: new Map([['story-1', found('Habit tracker bot')]]),
    ...overrides,
  }
}

const busy = worker('w1', { story_id: 'story-1', attempt_run: { id: 'run-1', status: 'running' } })

test('an active worker names its task and story, each linking to its page', () => {
  assert.deepEqual(workerWorkRow(busy, lookups()), {
    task: { state: 'found', text: 'Add the streak counter', href: '/tasks/task-1' },
    story: { state: 'found', text: 'Habit tracker bot', href: '/journeys/story-1' },
  })
})

test('a worker without an attempt run or story shows none for both', () => {
  const row = workerWorkRow(worker('idle'), lookups())
  assert.deepEqual(row.task, { state: 'none', text: 'none', href: null })
  assert.deepEqual(row.story, { state: 'none', text: 'none', href: null })
})

test('a run that carries no task shows none for the task', () => {
  const row = workerWorkRow(busy, lookups({ runTasks: new Map([['run-1', found<string | null>(null)]]) }))
  assert.equal(row.task.state, 'none')
  assert.equal(row.story.state, 'found')
})

test('pending and failed lookups show as such, never as a title or a link', () => {
  const pending = workerWorkRow(busy, lookups({ runTasks: new Map([['run-1', { state: 'pending' }]]) }))
  assert.deepEqual(pending.task, { state: 'pending', text: '…', href: null })

  const failedTask = workerWorkRow(busy, lookups({ taskTitles: new Map([['task-1', { state: 'failed' }]]) }))
  assert.deepEqual(failedTask.task, { state: 'failed', text: 'lookup failed', href: null })

  const failedStory = workerWorkRow(busy, lookups({ storyTitles: new Map([['story-1', { state: 'failed' }]]) }))
  assert.equal(failedStory.story.state, 'failed')
  assert.equal(failedStory.task.state, 'found')
})

test('a worker whose ids were never looked up is a defect, not a blank cell', () => {
  assert.throws(() => workerWorkRow(busy, lookups({ storyTitles: new Map() })), /story-1/)
})

test('lookup ids are collected once per run, story and task', () => {
  const workers = [busy, worker('w2', { story_id: 'story-1', attempt_run: { id: 'run-2', status: 'running' } }), worker('idle')]
  assert.deepEqual(workerLookupIds(workers), { runIds: ['run-1', 'run-2'], storyIds: ['story-1'] })
  const runTasks = new Map<string, Lookup<string | null>>([
    ['run-1', found<string | null>('task-1')],
    ['run-2', found<string | null>('task-1')],
    ['run-3', found<string | null>(null)],
    ['run-4', { state: 'pending' }],
  ])
  assert.deepEqual(runTaskIds(runTasks), ['task-1'])
})

test('a query reads as found once it has data, even through a later failed refetch', () => {
  assert.deepEqual(lookupOf({ data: undefined, isError: false }), { state: 'pending' })
  assert.deepEqual(lookupOf({ data: undefined, isError: true }), { state: 'failed' })
  assert.deepEqual(lookupOf({ data: 'Title', isError: true }), { state: 'found', value: 'Title' })
  assert.deepEqual(lookupOf<string | null>({ data: null, isError: false }), { state: 'found', value: null })
})
