import assert from 'node:assert/strict'
import test from 'node:test'

import type { AttentionItem, JourneyStep, ProductView, Topology } from '../src/types/api.ts'
import {
  focusStage,
  formatDuration,
  journeyLanes,
  journeyWindow,
  progressIndex,
  retries,
} from '../src/pages/journeyView.ts'
import {
  UNPLACED,
  groupByPlacement,
  highlight,
  packageUsage,
  productHealth,
  productPlacement,
  secretUsage,
} from '../src/pages/atlasModel.ts'
import { attentionLink, formatLeadTime, severityCounts } from '../src/pages/attentionView.ts'

const T0 = Date.parse('2026-10-07T14:00:00Z')
const at = (minutes: number) => new Date(T0 + minutes * 60_000).toISOString()

function step(stage: JourneyStep['stage'], overrides: Partial<JourneyStep> = {}): JourneyStep {
  return { stage, status: 'pending', started_at: null, finished_at: null, attempts: [], facts: [], ...overrides }
}

const completed: JourneyStep[] = [
  step('brief', { status: 'done', started_at: at(0), finished_at: at(4) }),
  step('plan', { status: 'done', started_at: at(4), finished_at: at(5) }),
  step('install', { status: 'skipped' }),
  step('build', {
    status: 'done',
    started_at: at(6),
    finished_at: at(16),
    attempts: [
      { id: 'eng-1', kind: 'engineering', status: 'failed', task_id: 't', actor: 'claude', started_at: at(6), finished_at: at(10), error: 'lint' },
      { id: 'eng-2', kind: 'engineering', status: 'completed', task_id: 't', actor: 'claude', started_at: at(11), finished_at: at(16), error: null },
    ],
  }),
  step('review', { status: 'done', started_at: at(16), finished_at: at(19) }),
  step('deploy', { status: 'done', started_at: at(19), finished_at: at(22) }),
  step('verify', { status: 'done', started_at: at(22), finished_at: at(25) }),
  step('live', { status: 'done', started_at: at(26), finished_at: at(26) }),
]

test('the timeline window spans first to last evidence of a finished journey', () => {
  assert.deepEqual(journeyWindow(completed, T0 + 999 * 60_000), { start: T0, end: T0 + 26 * 60_000 })
  assert.equal(journeyWindow([step('brief')], T0), null)
})

test('an open journey stretches the window to now', () => {
  const open = [step('brief', { status: 'done', started_at: at(0), finished_at: at(4) }), step('build', { status: 'waiting', started_at: at(5) })]
  assert.equal(journeyWindow(open, T0 + 60 * 60_000)?.end, T0 + 60 * 60_000)
})

test('lanes draw one bar per attempt and skip stages without evidence', () => {
  const window = journeyWindow(completed, T0)!
  const lanes = journeyLanes(completed, window, T0)
  assert.deepEqual(
    lanes.map((l) => l.stage),
    ['brief', 'plan', 'build', 'review', 'deploy', 'verify'],
  )
  const build = lanes.find((l) => l.stage === 'build')!
  assert.deepEqual(build.bars.map((b) => b.tone), ['failed', 'done'])
  assert.ok(Math.abs(build.bars[0].left - (6 / 26) * 100) < 0.01)
  for (const lane of lanes) for (const bar of lane.bars) assert.ok(bar.left + bar.width <= 100.0001)
})

test('focus opens the stuck station, else the last one reached', () => {
  assert.equal(focusStage(completed), 'live')
  const parked = completed.map((s) => (s.stage === 'deploy' ? { ...s, status: 'waiting' as const } : s))
  assert.equal(focusStage(parked), 'deploy')
})

test('progress, retries and durations read the steps as given', () => {
  assert.equal(progressIndex(completed), 7)
  assert.equal(progressIndex([step('brief', { status: 'done' }), step('plan', { status: 'active' }), step('install')]), 1)
  assert.equal(retries(completed[3]), 1)
  assert.equal(formatDuration(at(0), at(26), T0), '26 min')
  assert.equal(formatDuration(at(0), null, T0 + 125 * 60_000), '2 h 5 min')
  assert.equal(formatDuration(null, null, T0), '—')
})

function product(id: string, title: string, overrides: Partial<ProductView['passport']> = {}): ProductView {
  return {
    project_id: id,
    title,
    slug: title,
    status: 'active',
    latest_story_id: null,
    passport: { modules: ['backend', 'tg_bot'], packages: [], containers: [], user_secrets: [], ...overrides },
  }
}

const container = (name: string, placement: string, status = 'running') => ({
  name,
  status,
  placement,
  port: null,
  reserved_ram_mb: 256,
  response_time_ms: null,
  uptime_pct_24h: null,
  deployed_sha: null,
})

const topology: Topology = {
  placements: [
    { handle: 'mgmt-1', role: 'control', status: 'active', public_ip: '10.0.0.1', capacity_cpu: 4, capacity_ram_mb: 8192, used_ram_mb: 4000, cpu_usage_pct: 20, last_health_check: null },
    { handle: 'vps-b', role: 'product', status: 'active', public_ip: '10.0.0.3', capacity_cpu: 2, capacity_ram_mb: 4096, used_ram_mb: 100, cpu_usage_pct: null, last_health_check: null },
    { handle: 'vps-a', role: 'product', status: 'active', public_ip: '10.0.0.2', capacity_cpu: 2, capacity_ram_mb: 4096, used_ram_mb: 3000, cpu_usage_pct: null, last_health_check: null },
  ],
  products: [
    product('p2', 'weather', {
      containers: [container('backend', 'vps-a'), container('tg_bot', 'vps-a', 'degraded')],
      packages: [{ name: 'reminders', version: '1.3.0', kind: 'package' }, { name: 'textparse', version: '0.1.0', kind: 'library' }],
      user_secrets: ['OPENWEATHER_KEY'],
    }),
    product('p1', 'habits', {
      containers: [container('backend', 'vps-a')],
      packages: [{ name: 'reminders', version: '1.3.0', kind: 'package' }],
    }),
    product('p3', 'draft-bot'),
  ],
  platform_services: [],
}

test('products group by placement in a stable order with the unplaced last', () => {
  const groups = groupByPlacement(topology)
  assert.deepEqual(
    groups.map((g) => [g.handle, g.products.map((p) => p.title)]),
    [
      ['vps-a', ['habits', 'weather']],
      ['vps-b', []],
      [UNPLACED, ['draft-bot']],
    ],
  )
  assert.equal(productPlacement(topology.products[2]), UNPLACED)
})

test('selecting a package lights its products; selecting a product lights its parts', () => {
  const byPackage = highlight(topology, { kind: 'package', id: 'reminders' })!
  assert.deepEqual([...byPackage.products].sort(), ['p1', 'p2'])
  const byProduct = highlight(topology, { kind: 'product', id: 'p2' })!
  assert.deepEqual([...byProduct.products], ['p2'])
  assert.deepEqual([...byProduct.packages].sort(), ['reminders', 'textparse'])
  assert.deepEqual([...byProduct.secrets], ['OPENWEATHER_KEY'])
  assert.deepEqual([...highlight(topology, { kind: 'placement', id: UNPLACED })!.products], ['p3'])
  assert.equal(highlight(topology, null), null)
})

test('usage counts order components by how many products carry them', () => {
  assert.deepEqual(packageUsage(topology), [
    { name: 'reminders', count: 2 },
    { name: 'textparse', count: 1 },
  ])
  assert.deepEqual(secretUsage(topology), [{ name: 'OPENWEATHER_KEY', count: 1 }])
})

test('product health is its worst container', () => {
  assert.equal(productHealth(topology.products[0]), 'warn')
  assert.equal(productHealth(topology.products[1]), 'ok')
  assert.equal(productHealth(topology.products[2]), 'idle')
})

function item(overrides: Partial<AttentionItem>): AttentionItem {
  return {
    kind: 'task',
    severity: 'critical',
    title: 't',
    detail: null,
    since: null,
    project_id: null,
    project_title: null,
    story_id: null,
    task_id: null,
    application_id: null,
    server_handle: null,
    ...overrides,
  }
}

test('attention rows lead to the journey first, then the most specific page', () => {
  assert.equal(attentionLink(item({ story_id: 's', task_id: 't' })), '/journeys/s')
  assert.equal(attentionLink(item({ task_id: 't' })), '/tasks/t')
  assert.equal(attentionLink(item({ kind: 'application', application_id: 0 })), '/applications/0')
  assert.equal(attentionLink(item({ kind: 'incident', server_handle: 'vps-a' })), '/servers')
  assert.equal(attentionLink(item({ kind: 'queue' })), '/queues')
  assert.equal(attentionLink(item({ kind: 'incident' })), null)
  assert.deepEqual(severityCounts([item({}), item({ severity: 'info' })]), { critical: 1, warning: 0, info: 1 })
  assert.equal(formatLeadTime(null), '—')
  assert.equal(formatLeadTime(24), '24 min')
  assert.equal(formatLeadTime(90), '1.5 h')
})
