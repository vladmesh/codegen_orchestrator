import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card, CardTitle, CardValue } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import type { Project, Task, QueueHealth } from '@/types/api'

export function DashboardPage() {
  const projects = useQuery({
    queryKey: ['projects'],
    queryFn: () => api.get<Project[]>('/projects/'),
    refetchInterval: 30_000,
  })

  const tasks = useQuery({
    queryKey: ['tasks'],
    queryFn: () => api.get<Task[]>('/tasks/?limit=500'),
    refetchInterval: 30_000,
  })

  const queues = useQuery({
    queryKey: ['queues'],
    queryFn: () => api.raw<QueueHealth>('/debug/queues'),
    refetchInterval: 15_000,
  })

  const tasksByStatus = (tasks.data ?? []).reduce<Record<string, number>>((acc, t) => {
    acc[t.status] = (acc[t.status] ?? 0) + 1
    return acc
  }, {})

  const queueEntries = Object.entries(queues.data ?? {})
  const totalPending = queueEntries.reduce((sum, [, q]) => sum + (q.pending ?? 0), 0)

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Dashboard</h1>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <CardTitle>Projects</CardTitle>
          <CardValue>{projects.data?.length ?? '—'}</CardValue>
        </Card>
        <Card>
          <CardTitle>In Development</CardTitle>
          <CardValue>{tasksByStatus['in_dev'] ?? 0}</CardValue>
        </Card>
        <Card>
          <CardTitle>Blocked</CardTitle>
          <CardValue>{tasksByStatus['blocked'] ?? 0}</CardValue>
        </Card>
        <Card>
          <CardTitle>Queue Pending</CardTitle>
          <CardValue>{queues.data ? totalPending : '—'}</CardValue>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <h2 className="mb-4 text-lg font-semibold text-foreground">Tasks by Status</h2>
          {tasks.isLoading ? (
            <p className="text-muted-foreground">Loading...</p>
          ) : (
            <div className="space-y-2">
              {Object.entries(tasksByStatus)
                .sort(([, a], [, b]) => b - a)
                .map(([status, count]) => (
                  <div key={status} className="flex items-center justify-between">
                    <StatusBadge status={status} />
                    <span className="text-sm font-medium text-foreground">{count}</span>
                  </div>
                ))}
            </div>
          )}
        </Card>

        <Card>
          <h2 className="mb-4 text-lg font-semibold text-foreground">Queue Health</h2>
          {queues.isLoading ? (
            <p className="text-muted-foreground">Loading...</p>
          ) : (
            <div className="space-y-2">
              {queueEntries.map(([name, q]) => (
                <div key={name} className="flex items-center justify-between text-sm">
                  <span className="font-mono text-muted-foreground">{name}</span>
                  <div className="flex items-center gap-3">
                    <span className="text-foreground">len: {q.length}</span>
                    <span className={q.pending > 0 ? 'text-yellow-400' : 'text-muted-foreground'}>
                      pending: {q.pending}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
