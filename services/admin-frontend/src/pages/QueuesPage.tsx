import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import type { QueueHealth } from '@/types/api'

export function QueuesPage() {
  const { data: queues, isLoading } = useQuery({
    queryKey: ['queues'],
    queryFn: () => api.raw<QueueHealth>('/debug/queues'),
    refetchInterval: 10_000,
  })

  const entries = Object.entries(queues ?? {})

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Queues</h1>

      {isLoading ? (
        <p className="text-muted-foreground">Loading...</p>
      ) : entries.length === 0 ? (
        <p className="text-muted-foreground">No queues found</p>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {entries.map(([name, q]) => (
            <Card key={name}>
              <h3 className="font-mono text-sm font-medium text-foreground">{name}</h3>
              <div className="mt-3 grid grid-cols-3 gap-4 text-sm">
                <div>
                  <p className="text-muted-foreground">Length</p>
                  <p className="text-lg font-semibold text-foreground">{q.length}</p>
                </div>
                <div>
                  <p className="text-muted-foreground">Pending</p>
                  <p
                    className={`text-lg font-semibold ${q.pending > 0 ? 'text-yellow-400' : 'text-foreground'}`}
                  >
                    {q.pending}
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground">Consumers</p>
                  <p className="text-lg font-semibold text-foreground">{q.consumers}</p>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
