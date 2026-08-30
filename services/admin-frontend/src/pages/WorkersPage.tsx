import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '@/lib/api'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { relativeTime } from '@/lib/utils'
import type { EngineeringConsumerDrain, WorkerSummary } from '@/types/api'
import { requestEngineeringConsumerDrain, requestEngineeringConsumerResume } from './engineeringConsumerDrain'

function inventoryFact(value: string | null | undefined, error: string | null | undefined, absent = 'none') {
  return value ?? error ?? absent
}

export function WorkersPage() {
  const queryClient = useQueryClient()
  const { data: workers, isLoading } = useQuery({
    queryKey: ['workers'],
    queryFn: () => api.raw<WorkerSummary[]>('/wm-api/workers/'),
    refetchInterval: 5_000,
  })
  const drainQuery = useQuery({
    queryKey: ['engineering-consumer-drain'],
    queryFn: () => api.get<EngineeringConsumerDrain>('/engineering-consumer/drain'),
    refetchInterval: 5_000,
  })
  const drainMutation = useMutation({
    mutationFn: () => requestEngineeringConsumerDrain(api),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['engineering-consumer-drain'] }),
  })
  const resumeMutation = useMutation({
    mutationFn: () => requestEngineeringConsumerResume(api),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['engineering-consumer-drain'] }),
  })

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Workers</h1>

      <section className="flex items-center justify-between rounded-lg border border-border bg-muted/30 px-4 py-3">
        <div>
          <p className="font-medium text-foreground">
            Engineering consumer: {drainQuery.data?.draining ? 'draining' : 'accepting work'}
          </p>
          {drainQuery.data?.draining && (
            <p className="text-sm text-muted-foreground">
              Requested by {drainQuery.data.actor} {drainQuery.data.requested_at ? relativeTime(drainQuery.data.requested_at) : ''}
            </p>
          )}
        </div>
        {drainQuery.data?.draining ? (
          <button
            onClick={() => resumeMutation.mutate()}
            disabled={resumeMutation.isPending}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted disabled:opacity-50"
          >
            {resumeMutation.isPending ? 'Resuming...' : 'Resume consumer'}
          </button>
        ) : (
          <button
            onClick={() => drainMutation.mutate()}
            disabled={drainMutation.isPending}
            className="rounded-md bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-600 disabled:opacity-50"
          >
            {drainMutation.isPending ? 'Draining...' : 'Drain before deploy'}
          </button>
        )}
      </section>

      {isLoading ? (
        <p className="text-muted-foreground">Loading...</p>
      ) : (workers ?? []).length === 0 ? (
        <p className="text-muted-foreground">No active workers</p>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/50">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Worker</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Status</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Container</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Agent</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Lease</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Story Binding</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Attempt Run</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Waiting</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Project</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Error</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(workers ?? []).map((w) => (
                <tr key={w.id} className="hover:bg-muted/30">
                  <td className="px-4 py-3">
                    <Link
                      to={`/workers/${w.id}`}
                      className="font-mono text-sm font-medium text-primary hover:underline"
                    >
                      {w.id.slice(0, 12)}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={w.status.toLowerCase()} />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-foreground">
                    {inventoryFact(w.container?.state, w.container_error, 'absent')}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-foreground">
                    {inventoryFact(w.agent_process_status, w.agent_process_status_error, 'absent')}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-foreground">
                    {w.active_turn_lease?.request_id.slice(0, 12) ?? w.active_turn_lease_error ?? 'none'}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-foreground">
                    {w.story_bindings.length
                      ? w.story_bindings.map((story) => story.slice(0, 8)).join(', ')
                      : inventoryFact(null, w.story_bindings_error)}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-foreground">
                    {w.attempt_run
                      ? `${w.attempt_run.status}: ${w.attempt_run.id.slice(0, 12)}`
                      : inventoryFact(null, w.attempt_run_error)}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-foreground">
                    {w.waiting_attempt ? `${w.waiting_attempt.run_status}: ${w.waiting_attempt.run_id.slice(0, 12)}` : w.waiting_attempt_error ?? 'none'}
                  </td>
                  <td className="px-4 py-3">
                    {w.project_id ? (
                      <Link
                        to={`/projects/${w.project_id}`}
                        className="text-primary hover:underline"
                      >
                        {w.project_id.slice(0, 8)}
                      </Link>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </td>
                  <td className="max-w-xs truncate px-4 py-3 text-red-400">
                    {w.error ?? ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
