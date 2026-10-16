import type { WorkerSummary } from '../types/api'

/** One API read a Workers row depends on: still loading, failed, or answered. */
export type Lookup<T> = { state: 'pending' } | { state: 'failed' } | { state: 'found'; value: T }

/** A query's result as a lookup; data already read stays found through a later failed refetch. */
export function lookupOf<T>(query: { data: T | undefined; isError: boolean }): Lookup<T> {
  if (query.data !== undefined) return { state: 'found', value: query.data }
  return query.isError ? { state: 'failed' } : { state: 'pending' }
}

export interface WorkerWorkLookups {
  /** The task an attempt run belongs to, by run id; null for a run without a task. */
  runTasks: Map<string, Lookup<string | null>>
  /** Task title by task id. */
  taskTitles: Map<string, Lookup<string>>
  /** Story title by story id. */
  storyTitles: Map<string, Lookup<string>>
}

export interface WorkerWorkCell {
  state: 'found' | 'none' | 'pending' | 'failed'
  text: string
  href: string | null
}

export interface WorkerWorkRow {
  task: WorkerWorkCell
  story: WorkerWorkCell
}

const NONE: WorkerWorkCell = { state: 'none', text: 'none', href: null }
const PENDING: WorkerWorkCell = { state: 'pending', text: '…', href: null }
const FAILED: WorkerWorkCell = { state: 'failed', text: 'lookup failed', href: null }

function lookup<T>(table: Map<string, Lookup<T>>, id: string): Lookup<T> {
  const found = table.get(id)
  if (!found) throw new Error(`No lookup was requested for ${id}`)
  return found
}

function titleCell(title: Lookup<string>, href: string): WorkerWorkCell {
  if (title.state === 'pending') return PENDING
  if (title.state === 'failed') return FAILED
  return { state: 'found', text: title.value, href }
}

/** The attempt run ids and story ids a Workers list looks up, each once. */
export function workerLookupIds(workers: WorkerSummary[]): { runIds: string[]; storyIds: string[] } {
  const runIds = new Set<string>()
  const storyIds = new Set<string>()
  for (const worker of workers) {
    if (worker.attempt_run) runIds.add(worker.attempt_run.id)
    if (worker.story_id) storyIds.add(worker.story_id)
  }
  return { runIds: [...runIds], storyIds: [...storyIds] }
}

/** The task ids the answered run lookups name, each once. */
export function runTaskIds(runTasks: Map<string, Lookup<string | null>>): string[] {
  const taskIds = new Set<string>()
  for (const task of runTasks.values()) {
    if (task.state === 'found' && task.value !== null) taskIds.add(task.value)
  }
  return [...taskIds]
}

/** A worker's current task (through its attempt run) and story, each linking to its page. */
export function workerWorkRow(worker: WorkerSummary, lookups: WorkerWorkLookups): WorkerWorkRow {
  return { task: taskCell(worker, lookups), story: storyCell(worker, lookups) }
}

function taskCell(worker: WorkerSummary, lookups: WorkerWorkLookups): WorkerWorkCell {
  if (!worker.attempt_run) return NONE
  const taskId = lookup(lookups.runTasks, worker.attempt_run.id)
  if (taskId.state === 'pending') return PENDING
  if (taskId.state === 'failed') return FAILED
  if (taskId.value === null) return NONE
  return titleCell(lookup(lookups.taskTitles, taskId.value), `/tasks/${taskId.value}`)
}

function storyCell(worker: WorkerSummary, lookups: WorkerWorkLookups): WorkerWorkCell {
  if (!worker.story_id) return NONE
  return titleCell(lookup(lookups.storyTitles, worker.story_id), `/journeys/${worker.story_id}`)
}
