import type { JourneyAttempt, JourneyStage, JourneyStep, StepStatus } from '../types/api'

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

/** One lane per stage that did anything: a bar per attempt, or the step window itself. */
export function journeyLanes(steps: JourneyStep[], window: TimeWindow, now: number): Lane[] {
  const lanes: Lane[] = []
  for (const step of steps) {
    if (step.stage === 'live') continue
    const bars: Bar[] = []
    for (const attempt of step.attempts) {
      const from = ms(attempt.started_at)
      if (from === null) continue
      const to = ms(attempt.finished_at) ?? now
      bars.push(bar(attempt.id, from, to, window, attemptTone(attempt), `${attempt.kind} ${attempt.status}`))
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
