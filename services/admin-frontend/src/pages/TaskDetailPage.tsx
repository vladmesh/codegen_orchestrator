import { useParams, Link } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { formatDate } from '@/lib/utils'
import type { Task, TaskEvent } from '@/types/api'

export function TaskDetailPage() {
  const { id } = useParams<{ id: string }>()

  const { data: task, isLoading } = useQuery({
    queryKey: ['task', id],
    queryFn: () => api.get<Task>(`/tasks/${id}`),
    enabled: !!id,
  })

  const { data: events } = useQuery({
    queryKey: ['task-events', id],
    queryFn: () => api.get<TaskEvent[]>(`/tasks/${id}/events`),
    enabled: !!id,
  })

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!task) return <p className="text-muted-foreground">Task not found</p>

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/tasks" className="text-muted-foreground hover:text-foreground">
          Tasks
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="text-2xl font-bold text-foreground">{task.title}</h1>
        <StatusBadge status={task.status} />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <p className="text-sm text-muted-foreground">Type</p>
          <p className="mt-1 text-foreground">{task.type}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Priority</p>
          <p className="mt-1 text-foreground">{task.priority}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Iteration</p>
          <p className="mt-1 text-foreground">
            {task.current_iteration} / {task.max_iterations}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Created by</p>
          <p className="mt-1 text-foreground">{task.created_by}</p>
        </Card>
      </div>

      {task.description && (
        <Card>
          <h2 className="mb-2 text-sm font-medium text-muted-foreground">Description</h2>
          <pre className="whitespace-pre-wrap font-sans text-sm text-foreground">
            {task.description}
          </pre>
        </Card>
      )}

      {task.plan && (
        <Card>
          <h2 className="mb-2 text-sm font-medium text-muted-foreground">Plan</h2>
          <pre className="whitespace-pre-wrap font-mono text-xs text-foreground">{task.plan}</pre>
        </Card>
      )}

      <Card>
        <h2 className="mb-4 text-sm font-medium text-muted-foreground">
          Event Timeline ({events?.length ?? 0})
        </h2>
        {(events ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">No events yet</p>
        ) : (
          <div className="space-y-3">
            {(events ?? []).map((event) => (
              <div
                key={event.id}
                className="flex gap-4 border-l-2 border-border pl-4 text-sm"
              >
                <span className="shrink-0 text-muted-foreground">
                  {formatDate(event.created_at)}
                </span>
                <span className="font-medium text-foreground">{event.event_type}</span>
                {event.from_status && event.to_status && (
                  <span className="text-muted-foreground">
                    {event.from_status} → {event.to_status}
                  </span>
                )}
                <span className="text-muted-foreground">{event.actor}</span>
                {event.details && Object.keys(event.details).length > 0 && (
                  <span className="truncate text-muted-foreground">
                    {JSON.stringify(event.details)}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
