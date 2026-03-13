import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import type { DebugQueuesResponse } from '@/types/api'

export function QueuesPage() {
  const { data, isLoading } = useQuery({
    queryKey: ['queues'],
    queryFn: () => api.raw<DebugQueuesResponse>('/debug/queues'),
    refetchInterval: 10_000,
  })

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold text-foreground">Queues</h1>
        {data && (
          <span
            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
              data.status === 'ok'
                ? 'bg-green-900 text-green-200'
                : 'bg-yellow-900 text-yellow-200'
            }`}
          >
            {data.status}
          </span>
        )}
      </div>

      {data && data.issues.length > 0 && (
        <div className="rounded-lg border border-yellow-800 bg-yellow-950/30 p-4">
          <p className="mb-2 text-sm font-medium text-yellow-300">Issues detected</p>
          <ul className="space-y-1">
            {data.issues.map((issue, i) => (
              <li key={i} className="text-sm text-yellow-200">
                {issue}
              </li>
            ))}
          </ul>
        </div>
      )}

      {isLoading ? (
        <p className="text-muted-foreground">Loading...</p>
      ) : !data || data.bindings.length === 0 ? (
        <p className="text-muted-foreground">No queues found</p>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {data.bindings.map((b) => (
            <Card key={`${b.stream}-${b.group}`}>
              <div className="flex items-start justify-between">
                <div>
                  <h3 className="font-mono text-sm font-medium text-foreground">{b.stream}</h3>
                  <p className="text-xs text-muted-foreground">
                    {b.group} &mdash; {b.description}
                  </p>
                </div>
              </div>
              <div className="mt-3 grid grid-cols-4 gap-4 text-sm">
                <div>
                  <p className="text-muted-foreground">Length</p>
                  <p className="text-lg font-semibold text-foreground">{b.stream_info.length}</p>
                </div>
                <div>
                  <p className="text-muted-foreground">Pending</p>
                  <p
                    className={`text-lg font-semibold ${
                      b.group_info.pending > 0 ? 'text-yellow-400' : 'text-foreground'
                    }`}
                  >
                    {b.group_info.pending}
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground">Consumers</p>
                  <p className="text-lg font-semibold text-foreground">
                    {b.group_info.consumers}
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground">Last ID</p>
                  <p className="truncate font-mono text-xs text-muted-foreground">
                    {b.group_info.last_delivered_id ?? '-'}
                  </p>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
