import type { AdminOverview, RecentFailedRun } from '../types/api'

export type DashboardOverviewState = 'loading' | 'request_failed' | 'empty' | 'degraded' | 'healthy'

export function dashboardOverviewState(
  overview: AdminOverview | undefined,
  isLoading: boolean,
  isError: boolean,
): DashboardOverviewState {
  if (isLoading) return 'loading'
  if (isError || !overview) return 'request_failed'
  if (isEmptyOperationalOverview(overview)) return 'empty'
  return overview.queues.status === 'degraded' ? 'degraded' : 'healthy'
}

export function isEmptyOperationalOverview(overview: AdminOverview): boolean {
  return overview.queues.status === 'ok'
    && overview.queues.issues.length === 0
    && overview.queues.bindings.every(
      (binding) => binding.stream_info?.length === 0 && binding.group_info?.pending === 0,
    )
    && overview.paid_runs.queued === 0
    && overview.paid_runs.running === 0
    && overview.task_counts.waiting_human_review === 0
    && overview.task_counts.failed === 0
    && overview.recent_failed_runs.length === 0
}

export function completePendingCount(overview: AdminOverview): number | null {
  if (!overview.queues.bindings.every((binding) => binding.stream_info && binding.group_info)) {
    return null
  }
  return overview.queues.bindings.reduce(
    (total, binding) => total + (binding.group_info?.pending ?? 0),
    0,
  )
}

export function executorDecisionLabel(run: RecentFailedRun): string {
  if (run.executor_decision_availability === 'legacy') return 'Unavailable for legacy Run'
  if (run.executor_decision_availability === 'invalid') return 'Unavailable: invalid persisted snapshot'
  return run.executor_decision?.agent_type ?? 'Unavailable'
}
