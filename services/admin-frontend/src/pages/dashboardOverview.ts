import type { AdminOverview, RecentFailedRun } from '../types/api'

export type DashboardOverviewState = 'loading' | 'request_failed' | 'empty' | 'degraded' | 'healthy'

export function dashboardOverviewState(
  overview: AdminOverview | undefined,
  isLoading: boolean,
  isError: boolean,
): DashboardOverviewState {
  if (isLoading) return 'loading'
  if (isError || !overview) return 'request_failed'
  if (overview.queues.bindings.length === 0 && overview.recent_failed_runs.length === 0) return 'empty'
  return overview.queues.status === 'degraded' ? 'degraded' : 'healthy'
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
