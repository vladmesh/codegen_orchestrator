import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '@/lib/api'
import { Card, CardTitle, CardValue } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import type { AdminOverview, ExecutorDecision } from '@/types/api'
import {
  completePendingCount,
  dashboardOverviewState,
  executorDecisionLabel,
} from './dashboardOverview'

function ExecutorDecisionDetails({ decision }: { decision: ExecutorDecision | null }) {
  if (!decision) return null
  return (
    <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
      <dt>Executor</dt><dd>{decision.agent_type}</dd>
      <dt>Decision source</dt><dd>{decision.source}</dd>
      <dt>Policy version</dt><dd>{decision.policy_version}</dd>
      <dt>Reason</dt><dd>{decision.reason}</dd>
    </dl>
  )
}

export function DashboardPage() {
  const overview = useQuery({
    queryKey: ['admin-overview'],
    queryFn: () => api.get<AdminOverview>('/admin/overview'),
    refetchInterval: 15_000,
  })

  const state = dashboardOverviewState(overview.data, overview.isLoading, overview.isError)
  if (state === 'loading') return <p className="text-muted-foreground">Loading operational overview...</p>
  if (state === 'request_failed') {
    return <p role="alert" className="text-red-400">Operational overview request failed. Queue and work counts are unavailable.</p>
  }

  if (state === 'empty') {
    return <div className="space-y-6"><h1 className="text-2xl font-bold text-foreground">Dashboard</h1><p className="text-muted-foreground">No paid work, failed tasks, failed Runs, or pending queue work.</p></div>
  }
  if (!overview.data) {
    return <p role="alert" className="text-red-400">Operational overview request failed. Queue and work counts are unavailable.</p>
  }

  const { queues, task_counts: taskCounts, paid_runs: paidRuns, recent_failed_runs: failedRuns } = overview.data
  const totalPending = completePendingCount(overview.data)

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Dashboard</h1>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card><CardTitle>Paid queued</CardTitle><CardValue>{paidRuns.queued}</CardValue></Card>
        <Card><CardTitle>Paid running</CardTitle><CardValue>{paidRuns.running}</CardValue></Card>
        <Card><CardTitle>Waiting for human review</CardTitle><CardValue>{taskCounts.waiting_human_review}</CardValue></Card>
        <Card><CardTitle>Failed tasks</CardTitle><CardValue>{taskCounts.failed}</CardValue></Card>
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <h2 className="mb-4 text-lg font-semibold text-foreground">Queue health</h2>
          {queues.status === 'degraded' && <div role="alert" className="mb-3 rounded border border-yellow-800 bg-yellow-950/30 p-3 text-sm text-yellow-200"><p className="font-medium">Queue data is degraded. Counts below are not a complete snapshot.</p><ul className="mt-1 list-disc pl-5">{queues.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul></div>}
          {queues.bindings.length === 0 ? <p className="text-muted-foreground">No declared queue bindings.</p> : <div className="space-y-2"><div className="flex items-center justify-between text-sm"><span className="text-muted-foreground">Total pending</span><span className="font-medium text-foreground">{totalPending ?? 'Unavailable (degraded)'}</span></div>{queues.bindings.map((binding) => <div key={`${binding.stream}-${binding.group}`} className="flex items-center justify-between text-sm"><span className="font-mono text-muted-foreground">{binding.stream}</span><span className="text-foreground">len: {binding.stream_info?.length ?? 'Unavailable'}, pending: {binding.group_info?.pending ?? 'Unavailable'}</span></div>)}</div>}
        </Card>
        <Card>
          <h2 className="mb-4 text-lg font-semibold text-foreground">Paid executor decisions</h2>
          {Object.keys(paidRuns.by_executor).length === 0 ? <p className="text-muted-foreground">No queued or running paid Runs with persisted executor decisions.</p> : <div className="space-y-2">{Object.entries(paidRuns.by_executor).map(([executor, counts]) => <div key={executor} className="flex items-center justify-between text-sm"><span className="capitalize text-muted-foreground">{executor}</span><span className="text-foreground">queued: {counts.queued}, running: {counts.running}</span></div>)}</div>}
          {paidRuns.unavailable_executor_decisions > 0 && <p className="mt-3 text-sm text-yellow-300">{paidRuns.unavailable_executor_decisions} active paid Run decision snapshot(s) unavailable.</p>}
        </Card>
      </div>
      <Card>
        <h2 className="mb-4 text-lg font-semibold text-foreground">Recent failed Runs</h2>
        {failedRuns.length === 0 ? <p className="text-muted-foreground">No recent failed Runs.</p> : <div className="space-y-3">{failedRuns.map((run) => <article key={run.id} className="rounded border border-border p-3 text-sm"><div className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs text-muted-foreground">{run.id}</span><StatusBadge status="failed" /><span className="text-muted-foreground">{run.type}</span>{run.project_id && <Link to={`/projects/${run.project_id}`} className="text-primary hover:underline">Project</Link>}{run.task_id && <Link to={`/tasks/${run.task_id}`} className="text-primary hover:underline">Task</Link>}</div><p className="mt-2 whitespace-pre-wrap text-foreground">{run.error_message}</p><p className="mt-2 text-xs text-muted-foreground">Executor decision: {executorDecisionLabel(run)}</p><ExecutorDecisionDetails decision={run.executor_decision} /></article>)}</div>}
      </Card>
    </div>
  )
}
