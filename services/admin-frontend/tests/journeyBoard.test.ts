import assert from 'node:assert/strict'
import test from 'node:test'

import type { JourneyTimeline, TaskBoardAttempt, TaskBoardEntry, TimelineStage } from '../src/types/api.ts'
import { taskBoardColumns } from '../src/pages/taskBoard.ts'
import { timelineRows, timelineTotal } from '../src/pages/timelineView.ts'

const T0 = 1_700_000_000_000
const at = (minutes: number) => new Date(T0 + minutes * 60_000).toISOString()

function attempt(number: number, overrides: Partial<TaskBoardAttempt> = {}): TaskBoardAttempt {
  return {
    number,
    run_id: `run-${number}`,
    status: 'completed',
    worker_id: `worker-${number}`,
    actor: 'claude',
    started_at: at(0),
    finished_at: at(5),
    ...overrides,
  }
}

function task(id: string, overrides: Partial<TaskBoardEntry> = {}): TaskBoardEntry {
  return {
    task_id: id,
    title: `Task ${id}`,
    type: 'feature',
    status: 'backlog',
    column: 'backlog',
    origin: 'planned',
    attempts: [],
    worker_id: null,
    attempt_count: 0,
    ...overrides,
  }
}

const board: TaskBoardEntry[] = [
  task('t1'),
  task('t2', {
    status: 'blocked',
    column: 'in_development',
    attempts: [attempt(1, { status: 'running', finished_at: null })],
    worker_id: 'worker-1',
    attempt_count: 1,
  }),
  task('t3', {
    type: 'fix',
    status: 'in_ci',
    column: 'ci',
    origin: 'ci_fix',
    attempts: [attempt(1), attempt(2, { status: 'failed', worker_id: null, actor: null, started_at: at(10), finished_at: at(40) })],
    worker_id: null,
    attempt_count: 2,
  }),
  task('t4', { type: 'fix', status: 'done', column: 'done', origin: 'qa_fix', attempts: [attempt(1)], worker_id: 'worker-1', attempt_count: 1 }),
  task('t5', { status: 'failed', column: 'done' }),
]

test('the board has four columns in order, each holding its own tasks in board order', () => {
  const columns = taskBoardColumns(board, T0 + 30 * 60_000)
  assert.deepEqual(
    columns.map((c) => [c.label, c.cards.map((card) => card.taskId)]),
    [
      ['Backlog', ['t1']],
      ['In development', ['t2']],
      ['CI', ['t3']],
      ['Done', ['t4', 't5']],
    ],
  )
})

test('a task with no attempt shows no worker and zero attempts', () => {
  const [card] = taskBoardColumns([task('t1', { title: 'Add login', type: 'create' })], T0)[0].cards
  assert.equal(card.title, 'Add login')
  assert.equal(card.type, 'create')
  assert.equal(card.fixMark, null)
  assert.equal(card.unusualStatus, null)
  assert.equal(card.worker, 'no worker')
  assert.equal(card.attemptCount, 0)
  assert.deepEqual(card.attempts, [])
})

test('a CI fix and a QA fix carry their marks; a planned task carries none', () => {
  const marks = taskBoardColumns(board, T0)
    .flatMap((c) => c.cards)
    .map((card) => [card.taskId, card.fixMark])
  assert.deepEqual(marks, [
    ['t1', null],
    ['t2', null],
    ['t3', 'CI fix'],
    ['t4', 'QA fix'],
    ['t5', null],
  ])
})

test('a card shows its raw status only when the column does not usually hold it', () => {
  const statuses = taskBoardColumns(
    [
      task('a', { status: 'todo', column: 'backlog' }),
      task('b', { status: 'waiting_human_review', column: 'in_development' }),
      task('c', { status: 'testing', column: 'ci' }),
      task('d', { status: 'done', column: 'done' }),
      task('e', { status: 'cancelled', column: 'done' }),
    ],
    T0,
  )
    .flatMap((c) => c.cards)
    .map((card) => card.unusualStatus)
  assert.deepEqual(statuses, [null, 'waiting_human_review', null, null, 'cancelled'])
})

test('each attempt lists number, status, worker, actor and duration; an open one runs to now', () => {
  const columns = taskBoardColumns(board, T0 + 30 * 60_000)
  assert.deepEqual(columns[1].cards[0].attempts, [
    { key: 'run-1', number: 1, status: 'running', worker: 'worker-1', actor: 'claude', duration: '30 min' },
  ])
  assert.equal(columns[1].cards[0].worker, 'worker-1')
  assert.equal(columns[1].cards[0].unusualStatus, 'blocked')
  const fix = columns[2].cards[0]
  assert.equal(fix.attemptCount, 2)
  assert.equal(fix.worker, 'no worker')
  assert.deepEqual(fix.attempts, [
    { key: 'run-1', number: 1, status: 'completed', worker: 'worker-1', actor: 'claude', duration: '5 min' },
    { key: 'run-2', number: 2, status: 'failed', worker: '—', actor: '—', duration: '30 min' },
  ])
})

function stage(label: string, overrides: Partial<TimelineStage> = {}): TimelineStage {
  return {
    kind: 'engineering',
    label,
    started_at: at(0),
    finished_at: at(10),
    duration_seconds: 600,
    state: 'done',
    ...overrides,
  }
}

function timeline(overrides: Partial<JourneyTimeline> = {}): JourneyTimeline {
  return { stages: [], waits: [], total_seconds: null, total_unavailable: 'no_brief', ...overrides }
}

test('timeline rows keep stage order with done, running and unrecorded durations and a wait between stages', () => {
  const rows = timelineRows(
    timeline({
      stages: [
        stage('architect', { kind: 'architect', duration_seconds: 240 }),
        stage('engineering 1', { started_at: at(20), finished_at: null, duration_seconds: null, state: 'running' }),
        stage('deploy 1', { kind: 'deploy', started_at: null, finished_at: null, duration_seconds: null, state: 'unrecorded' }),
      ],
      waits: [{ after: 'architect', before: 'engineering 1', seconds: 960 }],
    }),
    T0 + 95 * 60_000,
  )
  assert.deepEqual(
    rows.map((row) => [row.kind, row.label, row.duration]),
    [
      ['stage', 'architect', '4 min'],
      ['wait', 'architect → engineering 1', '16 min'],
      ['stage', 'engineering 1', '1 h 15 min'],
      ['stage', 'deploy 1', 'not recorded'],
    ],
  )
})

test('a merge instant of zero seconds still reads as a duration', () => {
  const [row] = timelineRows(timeline({ stages: [stage('merge', { kind: 'merge', duration_seconds: 0 })] }), T0)
  assert.equal(row.duration, '<1 min')
})

test('the total runs from brief confirmed to QA passed when it is available', () => {
  assert.deepEqual(timelineTotal(timeline({ total_seconds: 2 * 3600 + 7 * 60, total_unavailable: null })), {
    kind: 'total',
    duration: '2 h 7 min',
  })
})

test('each reason for a missing total is shown instead of a number', () => {
  assert.deepEqual(
    (['no_brief', 'brief_not_confirmed', 'no_passed_qa'] as const).map((reason) =>
      timelineTotal(timeline({ total_unavailable: reason })),
    ),
    [
      { kind: 'unavailable', reason: 'No brief recorded' },
      { kind: 'unavailable', reason: 'Brief not confirmed yet' },
      { kind: 'unavailable', reason: 'No QA run has passed yet' },
    ],
  )
})
