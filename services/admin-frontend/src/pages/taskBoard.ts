/** Demo kanban of a story's tasks: pure grouping, no React, no clock reads. */
import type { JourneyStep, Task } from '../types/api'

export type BoardColumnId = 'backlog' | 'in_dev' | 'review' | 'in_ci' | 'qa' | 'done'

export interface BoardColumn {
  id: BoardColumnId
  label: string
  tasks: Task[]
}

/** Which column a task status lands in. Unknown statuses fall back to the backlog. */
const COLUMN_OF: Record<string, BoardColumnId> = {
  backlog: 'backlog',
  todo: 'backlog',
  waiting_resources: 'backlog',
  in_dev: 'in_dev',
  blocked: 'in_dev',
  failed: 'in_dev',
  waiting_human_review: 'review',
  in_review: 'review',
  in_ci: 'in_ci',
  testing: 'qa',
  done: 'done',
}

const BASE_COLUMNS: { id: BoardColumnId; label: string }[] = [
  { id: 'backlog', label: 'Backlog' },
  { id: 'in_dev', label: 'In Dev' },
  { id: 'review', label: 'Review' },
  { id: 'in_ci', label: 'CI' },
  { id: 'qa', label: 'QA' },
  { id: 'done', label: 'Done' },
]

/** Review and QA are shown only when some task is actually there; the other four always are. */
const OPTIONAL: ReadonlySet<BoardColumnId> = new Set(['review', 'qa'])

export function columnOf(status: string): BoardColumnId {
  return COLUMN_OF[status] ?? 'backlog'
}

/** Columns with their cards; `occupied` forces optional columns that hold a non-task card. */
export function boardColumns(tasks: Task[], occupied: BoardColumnId[] = []): BoardColumn[] {
  const sorted = [...tasks].sort((a, b) => a.priority - b.priority)
  return BASE_COLUMNS.map((c) => ({ ...c, tasks: sorted.filter((t) => columnOf(t.status) === c.id) })).filter(
    (c) => !OPTIONAL.has(c.id) || c.tasks.length > 0 || occupied.includes(c.id),
  )
}

/** A task created to repair something: a bugfix type or a "Fix:" title. */
export function isFixTask(task: Pick<Task, 'type' | 'title'>): boolean {
  return task.type === 'bugfix' || /^fix\b/i.test(task.title)
}

/** Short card badge text for a task status. */
export function cardStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    backlog: 'Planned',
    todo: 'Queued',
    in_dev: 'Coding',
    in_ci: 'Testing',
    testing: 'QA',
    done: 'Done',
    blocked: 'Blocked',
    failed: 'Failed',
  }
  return labels[status] ?? status.replace(/_/g, ' ')
}

/** Card title without a leading "Fix:" — the card shows a fix tag instead. */
export function cardTitle(task: Pick<Task, 'type' | 'title'>): string {
  return isFixTask(task) ? task.title.replace(/^fix\s*:\s*/i, '') : task.title
}

export type PrTone = 'running' | 'failed' | 'passed'

export interface PrCard {
  column: BoardColumnId
  title: string
  status: string
  detail: string | null
  tone: PrTone
}

/**
 * The story's pull request as a board card, from the journey steps: in CI while the PR
 * is checked (red after a failed run), in QA while the live bot is verified, Done once merged.
 */
export function prCard(steps: JourneyStep[] | undefined, prNumber: number | null): PrCard | null {
  const review = steps?.find((s) => s.stage === 'review')
  if (!review || review.status === 'pending' || review.status === 'skipped') return null
  const runs = review.attempts.filter((a) => !a.task_id && a.kind === 'engineering')
  const last = runs[runs.length - 1]
  const title = prNumber !== null ? `Pull request #${prNumber}` : 'Pull request'
  const verify = steps?.find((s) => s.stage === 'verify')
  if (verify?.status === 'active') return { column: 'qa', title, status: 'Live checks', detail: null, tone: 'running' }
  if (review.status === 'done') {
    const detail = runs.length > 0 ? `CI run ${runs.length} passed` : null
    return { column: 'done', title, status: 'Merged', detail, tone: 'passed' }
  }
  if (!last) return { column: 'in_ci', title, status: 'Waiting for CI', detail: null, tone: 'running' }
  const name = `CI run ${runs.length}`
  if (last.status === 'running') return { column: 'in_ci', title, status: `${name} running`, detail: null, tone: 'running' }
  if (last.status === 'failed') return { column: 'in_ci', title, status: `${name} failed`, detail: last.error, tone: 'failed' }
  return { column: 'in_ci', title, status: `${name} passed`, detail: null, tone: 'passed' }
}
