import type { JourneyAttempt, JourneyStage, JourneyStep, StepStatus, StoryLink, StoryLinkKind } from '../types/api'

export const STAGE_LABELS: Record<JourneyStage, string> = {
  brief: 'Brief',
  plan: 'Plan',
  install: 'Catalog',
  build: 'Code',
  review: 'PR + CI',
  deploy: 'Deploy',
  verify: 'QA',
  live: 'Live',
}

// Who does the work of a stage; the lanes under the line are one per actor.
export const STAGE_ACTORS: Record<JourneyStage, string> = {
  brief: 'Product Owner',
  plan: 'Architect',
  install: 'Scaffolder',
  build: 'Coding worker',
  review: 'GitHub CI',
  deploy: 'Deploy worker',
  verify: 'QA worker',
  live: 'Product',
}

export type BarTone = 'done' | 'failed' | 'active' | 'waiting'

export interface Bar {
  key: string
  left: number
  width: number
  tone: BarTone
  label: string
  /** Short text drawn inside the bar when it is wide enough (demo build). */
  caption?: string
}

export interface Lane {
  stage: JourneyStage
  actor: string
  bars: Bar[]
}

export interface TimeWindow {
  start: number
  end: number
}

const MIN_BAR_PCT = 0.8

function ms(iso: string | null): number | null {
  return iso ? new Date(iso).getTime() : null
}

/** The span the timeline draws: first evidence to last, or to now while the journey runs. */
export function journeyWindow(steps: JourneyStep[], now: number): TimeWindow | null {
  const starts = steps.map((s) => ms(s.started_at)).filter((v): v is number => v !== null)
  if (starts.length === 0) return null
  const open = steps.some((s) => s.status === 'active' || s.status === 'waiting')
  const ends = steps
    .flatMap((s) => [ms(s.finished_at), ms(s.started_at)])
    .filter((v): v is number => v !== null)
  const end = open ? now : Math.max(...ends)
  const start = Math.min(...starts)
  return { start, end: Math.max(end, start + 60_000) }
}

function attemptTone(attempt: JourneyAttempt): BarTone {
  if (attempt.status === 'queued' || attempt.status === 'running') return 'active'
  if (attempt.status === 'completed' || attempt.status === 'published') return 'done'
  return 'failed'
}

function stepTone(status: StepStatus): BarTone {
  if (status === 'failed') return 'failed'
  if (status === 'waiting') return 'waiting'
  if (status === 'active') return 'active'
  return 'done'
}

function bar(key: string, from: number, to: number, window: TimeWindow, tone: BarTone, label: string): Bar {
  const span = window.end - window.start
  const left = ((from - window.start) / span) * 100
  const width = Math.max(((to - from) / span) * 100, MIN_BAR_PCT)
  return { key, left: Math.max(0, Math.min(left, 100 - MIN_BAR_PCT)), width: Math.min(width, 100), tone, label }
}

/** A task run inside PR + CI: the developer reworking the code after a failed check. */
function isRework(step: JourneyStep, attempt: JourneyAttempt): boolean {
  return step.stage === 'review' && attempt.task_id !== null
}

/**
 * One lane per stage that did anything: a bar per attempt, or the step window itself.
 * Rework after a failed CI run is developer work, so it is drawn in the Code (Developer)
 * lane; the PR + CI lane keeps only the CI runs: CI ✗, a gap while the fix is coded, CI ✓.
 */
export function journeyLanes(steps: JourneyStep[], window: TimeWindow, now: number): Lane[] {
  const lanes: Lane[] = []
  const reworks = steps.flatMap((step) => step.attempts.filter((a) => isRework(step, a)))
  for (const step of steps) {
    if (step.stage === 'live') continue
    const bars: Bar[] = []
    const attempts = step.attempts.filter((a) => !isRework(step, a))
    if (step.stage === 'build') attempts.push(...reworks)
    for (const attempt of attempts) {
      const from = ms(attempt.started_at)
      if (from === null) continue
      const to = ms(attempt.finished_at) ?? now
      const tone = attemptTone(attempt)
      const b = bar(attempt.id, from, to, window, tone, `${attempt.kind} ${attempt.status}`)
      if (step.stage === 'review') {
        b.caption = `CI ${tone === 'failed' ? '✗' : tone === 'done' ? '✓' : '…'}`
      }
      bars.push(b)
    }
    if (bars.length === 0) {
      const from = ms(step.started_at)
      if (from !== null) {
        const to = ms(step.finished_at) ?? (step.status === 'active' || step.status === 'waiting' ? now : from)
        bars.push(bar(step.stage, from, to, window, stepTone(step.status), STAGE_LABELS[step.stage]))
      }
    }
    if (bars.length > 0) lanes.push({ stage: step.stage, actor: STAGE_ACTORS[step.stage], bars })
  }
  return lanes
}

/** The station to open first: where the journey is stuck or running, else where it ended. */
export function focusStage(steps: JourneyStep[]): JourneyStage {
  const open = steps.find((s) => s.status === 'waiting' || s.status === 'failed' || s.status === 'active')
  if (open) return open.stage
  const reached = steps.filter((s) => s.status === 'done')
  return reached.length > 0 ? reached[reached.length - 1].stage : steps[0].stage
}

export function formatDuration(fromIso: string | null, toIso: string | null, now: number): string {
  const from = ms(fromIso)
  if (from === null) return '—'
  const minutes = Math.round(((ms(toIso) ?? now) - from) / 60_000)
  if (minutes < 1) return '<1 min'
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours} h ${minutes % 60} min`
  return `${Math.floor(hours / 24)} d`
}

export function retries(step: JourneyStep): number {
  return step.attempts.filter((a) => attemptTone(a) === 'failed').length
}

/** Index of the furthest station the journey has reached; the line is drawn up to it. */
export function progressIndex(steps: JourneyStep[]): number {
  let index = 0
  steps.forEach((step, i) => {
    if (step.status !== 'pending' && step.status !== 'skipped') index = i
  })
  return index
}

export const LINK_KIND_LABELS: Record<StoryLinkKind, string> = {
  module_proposal: 'Reusable module proposed',
}

export interface LinkedPullRequest {
  key: number
  label: string
  title: string
  url: string
  repo: string | null
  number: string | null
  createdAt: string
}

const PULL_URL = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)$/

/** The linked pull requests as the card lists them, oldest first, with `owner/repo` and `#n` from the URL. */
export function linkedPullRequests(links: StoryLink[]): LinkedPullRequest[] {
  return [...links]
    .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at) || a.id - b.id)
    .map((link) => {
      const match = PULL_URL.exec(link.url)
      return {
        key: link.id,
        label: LINK_KIND_LABELS[link.kind] ?? link.kind,
        title: link.title,
        url: link.url,
        repo: match ? match[1] : null,
        number: match ? `#${match[2]}` : null,
        createdAt: link.created_at,
      }
    })
}
