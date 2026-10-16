import type { JourneyTimeline, TimelineStage, TimelineStageState, TimelineTotalUnavailable } from '../types/api'
import { formatDuration, formatSeconds } from './journeyView.ts'

export type TimelineRow =
  | { kind: 'stage'; key: string; label: string; state: TimelineStageState; duration: string }
  | { kind: 'wait'; key: string; label: string; duration: string }

export type TimelineTotal = { kind: 'total'; duration: string } | { kind: 'unavailable'; reason: string }

export const TOTAL_UNAVAILABLE_REASONS: Record<TimelineTotalUnavailable, string> = {
  no_brief: 'No brief recorded',
  brief_not_confirmed: 'Brief not confirmed yet',
  no_passed_qa: 'No QA run has passed yet',
}

function stageDuration(stage: TimelineStage, now: number): string {
  if (stage.state === 'unrecorded') return 'not recorded'
  if (stage.state === 'running') return formatDuration(stage.started_at, null, now)
  if (stage.duration_seconds === null) throw new Error(`done stage ${stage.label} has no duration`)
  return formatSeconds(stage.duration_seconds)
}

/** Stages in the order given, each wait placed right before the stage it ends at; `now` times running stages. */
export function timelineRows(timeline: JourneyTimeline, now: number): TimelineRow[] {
  const rows: TimelineRow[] = []
  let next = 0
  timeline.stages.forEach((stage, index) => {
    const wait = timeline.waits[next]
    if (wait && wait.before === stage.label) {
      rows.push({ kind: 'wait', key: `wait-${next}`, label: `${wait.after} → ${wait.before}`, duration: formatSeconds(wait.seconds) })
      next += 1
    }
    rows.push({ kind: 'stage', key: `stage-${index}`, label: stage.label, state: stage.state, duration: stageDuration(stage, now) })
  })
  if (next < timeline.waits.length) throw new Error(`wait before ${timeline.waits[next].before} matches no stage`)
  return rows
}

/** The total from brief confirmed to QA passed, or why the story has none yet. */
export function timelineTotal(timeline: JourneyTimeline): TimelineTotal {
  if (timeline.total_unavailable !== null) {
    return { kind: 'unavailable', reason: TOTAL_UNAVAILABLE_REASONS[timeline.total_unavailable] }
  }
  if (timeline.total_seconds === null) throw new Error('timeline has neither a total nor a reason')
  return { kind: 'total', duration: formatSeconds(timeline.total_seconds) }
}
