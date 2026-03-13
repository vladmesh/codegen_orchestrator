import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '@/lib/api'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { relativeTime } from '@/lib/utils'
import type { WorkerSummary } from '@/types/api'

export function WorkersPage() {
  const { data: workers, isLoading } = useQuery({
    queryKey: ['workers'],
    queryFn: () => api.raw<WorkerSummary[]>('/wm-api/workers/'),
    refetchInterval: 5_000,
  })

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Workers</h1>

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
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Project</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Last Activity
                </th>
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
                  <td className="px-4 py-3 text-muted-foreground">
                    {w.last_activity ? relativeTime(w.last_activity) : '-'}
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
