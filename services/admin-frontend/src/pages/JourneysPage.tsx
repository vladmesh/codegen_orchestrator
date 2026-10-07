import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/utils'
import { StatusBadge } from '@/components/ui/StatusBadge'
import type { JourneySummary } from '@/types/api'
import { STAGE_LABELS, formatDuration } from './journeyView'

export function JourneysPage() {
  const journeys = useQuery({
    queryKey: ['admin-console-journeys'],
    queryFn: () => api.get<JourneySummary[]>('/admin/v2/journeys'),
    refetchInterval: 15_000,
  })

  if (journeys.isLoading) return <p className="text-muted-foreground">Loading…</p>
  if (journeys.isError || !journeys.data) {
    return <p role="alert" className="text-red-400">Journey list request failed.</p>
  }
  const now = journeys.dataUpdatedAt

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold text-foreground">Journeys</h1>
        <p className="text-sm text-muted-foreground">
          Every story from the user's request to a running deploy, most recently touched first.
        </p>
      </div>
      {journeys.data.length === 0 ? (
        <p className="rounded-lg border border-border p-6 text-muted-foreground">No stories yet.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-border text-left font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                <th className="px-3 py-2 font-medium">Story</th>
                <th className="px-3 py-2 font-medium">Product</th>
                <th className="px-3 py-2 font-medium">Stage</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Took</th>
                <th className="px-3 py-2 font-medium">Touched</th>
              </tr>
            </thead>
            <tbody>
              {journeys.data.map((journey) => (
                <tr key={journey.story_id} className="border-b border-border/60 last:border-b-0 hover:bg-accent/40">
                  <td className="px-3 py-2.5">
                    <Link to={`/journeys/${journey.story_id}`} className="text-foreground hover:underline">
                      {journey.title}
                    </Link>
                  </td>
                  <td className="px-3 py-2.5 text-muted-foreground">{journey.project_title}</td>
                  <td className="px-3 py-2.5 font-mono text-xs">
                    {journey.current_stage ? STAGE_LABELS[journey.current_stage] : '—'}
                    {journey.waiting_on !== 'none' && (
                      <span className="ml-2 text-amber-400">waits on {journey.waiting_on.replace('_', ' ')}</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    <StatusBadge status={journey.status} />
                  </td>
                  <td className="px-3 py-2.5 tabular-nums text-muted-foreground">
                    {formatDuration(journey.created_at, journey.finished_at, now)}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-muted-foreground">{relativeTime(journey.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
