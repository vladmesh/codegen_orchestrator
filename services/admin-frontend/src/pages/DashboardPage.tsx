import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { langfuseUrl } from '@/lib/langfuse'
import { Card, CardTitle, CardValue } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import type { Project, Task, QueueHealth, LangfuseTracesResponse } from '@/types/api'

const AGENT_COLORS: Record<string, string> = {
  po: 'bg-blue-500/20 text-blue-400',
  architect: 'bg-purple-500/20 text-purple-400',
  engineering: 'bg-green-500/20 text-green-400',
  deploy: 'bg-orange-500/20 text-orange-400',
}

function agentFromTags(tags: string[]): string | null {
  const tag = tags.find((t) => t.startsWith('agent:'))
  return tag ? tag.slice(6) : null
}

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 60_000) return `${Math.round(ms / 1000)}s ago`
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m ago`
  if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)}h ago`
  return `${Math.round(ms / 86_400_000)}d ago`
}

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

  const traces = useQuery({
    queryKey: ['langfuse-traces'],
    queryFn: () => api.raw<LangfuseTracesResponse>('/langfuse-api/traces?limit=10'),
    refetchInterval: 30_000,
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

      <Card>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-foreground">Recent LLM Traces</h2>
          <a
            href={langfuseUrl()}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm text-muted-foreground hover:text-foreground"
          >
            Open Langfuse &rarr;
          </a>
        </div>
        {traces.isLoading ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : traces.isError ? (
          <p className="text-sm text-muted-foreground">Langfuse unavailable</p>
        ) : (
          <div className="space-y-2">
            {(traces.data?.data ?? []).map((t) => {
              const agent = agentFromTags(t.tags)
              const colorClass = agent ? AGENT_COLORS[agent] ?? 'bg-zinc-700 text-zinc-300' : null
              return (
                <a
                  key={t.id}
                  href={langfuseUrl(t.htmlPath)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between rounded px-2 py-1.5 text-sm hover:bg-zinc-800/50"
                >
                  <div className="flex items-center gap-2">
                    {colorClass && (
                      <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${colorClass}`}>
                        {agent}
                      </span>
                    )}
                    <span className="text-muted-foreground">
                      {t.userId ? `user:${t.userId}` : 'system'}
                    </span>
                  </div>
                  <div className="flex items-center gap-3">
                    {t.latency != null && (
                      <span className="font-mono text-muted-foreground">
                        {t.latency.toFixed(1)}s
                      </span>
                    )}
                    <span className="text-muted-foreground">{timeAgo(t.timestamp)}</span>
                  </div>
                </a>
              )
            })}
            {(traces.data?.data ?? []).length === 0 && (
              <p className="text-sm text-muted-foreground">No traces yet</p>
            )}
          </div>
        )}
      </Card>
    </div>
  )
}
