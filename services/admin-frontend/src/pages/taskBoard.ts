import type { TaskBoardColumn, TaskBoardEntry, TaskOrigin, TaskStatus } from '../types/api'
import { formatDuration } from './journeyView.ts'

export const BOARD_COLUMNS: { id: TaskBoardColumn; label: string }[] = [
  { id: 'backlog', label: 'Backlog' },
  { id: 'in_development', label: 'In development' },
  { id: 'ci', label: 'CI' },
  { id: 'done', label: 'Done' },
]

// The statuses a column holds without comment; any other status in it is shown on the card.
const USUAL_STATUSES: Record<TaskBoardColumn, readonly TaskStatus[]> = {
  backlog: ['backlog', 'todo'],
  in_development: ['in_dev'],
  ci: ['in_ci', 'testing'],
  done: ['done'],
}

const FIX_MARKS: Record<TaskOrigin, string | null> = {
  planned: null,
  ci_fix: 'CI fix',
  qa_fix: 'QA fix',
}

export interface BoardAttempt {
  key: string
  number: number
  status: string
  worker: string
  actor: string
  duration: string
}

export interface BoardCard {
  taskId: string
  title: string
  type: TaskBoardEntry['type']
  fixMark: string | null
  unusualStatus: TaskStatus | null
  worker: string
  attemptCount: number
  attempts: BoardAttempt[]
}

export interface BoardColumnGroup {
  id: TaskBoardColumn
  label: string
  cards: BoardCard[]
}

function boardCard(entry: TaskBoardEntry, now: number): BoardCard {
  return {
    taskId: entry.task_id,
    title: entry.title,
    type: entry.type,
    fixMark: FIX_MARKS[entry.origin],
    unusualStatus: USUAL_STATUSES[entry.column].includes(entry.status) ? null : entry.status,
    worker: entry.worker_id ?? 'no worker',
    attemptCount: entry.attempt_count,
    attempts: entry.attempts.map((attempt) => ({
      key: attempt.run_id,
      number: attempt.number,
      status: attempt.status,
      worker: attempt.worker_id ?? '—',
      actor: attempt.actor ?? '—',
      duration: formatDuration(attempt.started_at, attempt.finished_at, now),
    })),
  }
}

/** The four board columns in order, each with its tasks in board order; `now` times open attempts. */
export function taskBoardColumns(board: TaskBoardEntry[], now: number): BoardColumnGroup[] {
  return BOARD_COLUMNS.map((column) => ({
    ...column,
    cards: board.filter((entry) => entry.column === column.id).map((entry) => boardCard(entry, now)),
  }))
}
