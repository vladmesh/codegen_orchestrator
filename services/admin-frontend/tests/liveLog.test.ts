import assert from 'node:assert/strict'
import test from 'node:test'

import { ApiError } from '../src/lib/api.ts'
import type { LiveLogKind, LiveLogLine, LiveLogSource, LiveLogUnavailable, StoryStatus } from '../src/types/api.ts'
import {
  isTerminalStory,
  liveLogInterval,
  liveLogRows,
  omittedMessage,
  unavailableMessage,
  workerLogLive,
} from '../src/pages/liveLogView.ts'

const T0 = 1_700_000_000_000
const at = (seconds: number) => new Date(T0 + seconds * 1000).toISOString()

function line(seq: number, overrides: Partial<LiveLogLine> = {}): LiveLogLine {
  return { at: at(seq), source: 'developer', attempt_id: 'run-1', seq, kind: 'info', text: `line ${seq}`, ...overrides }
}

test('every source gets its terminal tag', () => {
  const tags: Record<LiveLogSource, string> = { architect: 'architect', developer: 'dev', qa: 'qa', worker: 'worker' }
  for (const [source, tag] of Object.entries(tags) as [LiveLogSource, string][]) {
    const [row] = liveLogRows([line(0, { source })], 'UTC')
    assert.equal(row.tag, tag, source)
    assert.equal(row.source, source)
  }
})

test('every kind gets its tone and only a command gets a prompt', () => {
  const tones: Record<LiveLogKind, string> = {
    step: 'heading',
    info: 'plain',
    cmd: 'command',
    file: 'path',
    ok: 'success',
    err: 'error',
  }
  for (const [kind, tone] of Object.entries(tones) as [LiveLogKind, string][]) {
    const [row] = liveLogRows([line(0, { kind, text: 'pytest -q' })], 'UTC')
    assert.equal(row.tone, tone, kind)
    assert.equal(row.prompt, kind === 'cmd' ? '$ ' : null, kind)
    assert.equal(row.text, 'pytest -q')
  }
})

test('rows keep the API order, are keyed by attempt and seq, and show the clock time', () => {
  const rows = liveLogRows(
    [line(3, { attempt_id: 'run-1' }), line(0, { attempt_id: 'run-2', at: at(4) }), line(4, { attempt_id: 'run-1', at: at(65) })],
    'UTC',
  )
  assert.deepEqual(rows.map((r) => r.key), ['run-1:3', 'run-2:0', 'run-1:4'])
  assert.deepEqual(rows.map((r) => r.time), ['22:13:23', '22:13:24', '22:14:25'])
})

test('a UTC timestamp reads the same with Z, +00:00 or no designator', () => {
  const iso = at(0)
  const bare = iso.replace('Z', '')
  const times = liveLogRows([line(0, { at: iso }), line(1, { at: `${bare}+00:00` }), line(2, { at: bare })], 'Europe/Berlin')
  assert.equal(new Set(times.map((r) => r.time)).size, 1)
})

test('each unavailable reason has its own message', () => {
  const reasons: LiveLogUnavailable[] = ['log_store_not_configured', 'log_store_query_failed', 'log_store_timed_out']
  const messages = reasons.map(unavailableMessage)
  assert.ok(messages.every((m) => m.length > 0))
  assert.equal(new Set(messages).size, reasons.length)
})

test('omitted attempts are announced only when there are some', () => {
  assert.equal(omittedMessage(0), null)
  assert.match(omittedMessage(1) ?? '', /oldest attempt is not shown/)
  assert.match(omittedMessage(3) ?? '', /3 oldest attempts are not shown/)
})

test('the log polls within five seconds while live and stops once the run cannot write', () => {
  const interval = liveLogInterval(true)
  assert.equal(typeof interval, 'number')
  assert.ok((interval as number) > 0 && (interval as number) <= 5_000)
  assert.equal(liveLogInterval(false), false)
})

test('only completed, failed and archived stories are terminal', () => {
  const statuses: Record<StoryStatus, boolean> = {
    created: false,
    in_progress: false,
    reopened: false,
    pr_review: false,
    deploying: false,
    testing: false,
    waiting_human_review: false,
    waiting_user_secret: false,
    completed: true,
    failed: true,
    archived: true,
  }
  for (const [status, terminal] of Object.entries(statuses) as [StoryStatus, boolean][]) {
    assert.equal(isTerminalStory(status), terminal, status)
  }
})

test('a worker log polls through a transient failed read and stops on a 404', () => {
  const worker = { id: 'w-1' }
  assert.equal(workerLogLive({ data: worker, error: null }), true)
  assert.equal(workerLogLive({ data: worker, error: new ApiError(503, '503 Service Unavailable') }), true)
  assert.equal(workerLogLive({ data: worker, error: new TypeError('Failed to fetch') }), true)
  assert.equal(workerLogLive({ data: worker, error: new ApiError(404, '404 Not Found') }), false)
})

test('a worker log never polls before the worker read has answered', () => {
  assert.equal(workerLogLive({ data: undefined, error: null }), false)
  assert.equal(workerLogLive({ data: undefined, error: new ApiError(500, '500 Internal Server Error') }), false)
})
