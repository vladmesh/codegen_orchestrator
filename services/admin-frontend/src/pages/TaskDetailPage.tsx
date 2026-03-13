import { useState } from 'react'
import { useParams, Link } from 'react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { formatDate } from '@/lib/utils'
import type { Task, TaskEvent } from '@/types/api'

export function TaskDetailPage() {
  const { id } = useParams<{ id: string }>()
  const queryClient = useQueryClient()

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

  const invalidateTask = () => {
    queryClient.invalidateQueries({ queryKey: ['task', id] })
    queryClient.invalidateQueries({ queryKey: ['task-events', id] })
  }

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!task) return <p className="text-muted-foreground">Task not found</p>

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link to="/tasks" className="text-muted-foreground hover:text-foreground">
            Tasks
          </Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="text-2xl font-bold text-foreground">{task.title}</h1>
          <StatusBadge status={task.status} />
        </div>
        <TaskActions task={task} onSuccess={invalidateTask} />
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

/* ---------- Task Action Buttons ---------- */

function TaskActions({ task, onSuccess }: { task: Task; onSuccess: () => void }) {
  const [showResume, setShowResume] = useState(false)
  const [guidance, setGuidance] = useState('')
  const [confirmRetry, setConfirmRetry] = useState(false)

  const retryMutation = useMutation({
    mutationFn: () =>
      api.post<Task>(`/tasks/${task.id}/transition?to_status=backlog`, {
        actor: 'admin',
        details: { action: 'retry_from_admin' },
      }),
    onSuccess: () => {
      setConfirmRetry(false)
      onSuccess()
    },
  })

  const resumeMutation = useMutation({
    mutationFn: () =>
      api.post<Task>(`/tasks/${task.id}/resume`, {
        actor: 'admin',
        guidance,
      }),
    onSuccess: () => {
      setShowResume(false)
      setGuidance('')
      onSuccess()
    },
  })

  return (
    <div className="flex items-center gap-2">
      {/* Retry: failed → backlog */}
      {task.status === 'failed' && (
        <>
          {confirmRetry ? (
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Retry this task?</span>
              <button
                onClick={() => retryMutation.mutate()}
                disabled={retryMutation.isPending}
                className="rounded-md bg-blue-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50"
              >
                {retryMutation.isPending ? 'Retrying...' : 'Confirm'}
              </button>
              <button
                onClick={() => setConfirmRetry(false)}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmRetry(true)}
              className="rounded-md border border-blue-800 px-3 py-1.5 text-sm text-blue-400 hover:bg-blue-900/30"
            >
              Retry
            </button>
          )}
        </>
      )}

      {/* Resume: waiting_human_review → in_dev with guidance */}
      {task.status === 'waiting_human_review' && (
        <>
          {showResume ? (
            <div className="flex items-center gap-2">
              <textarea
                value={guidance}
                onChange={(e) => setGuidance(e.target.value)}
                placeholder="Guidance for the worker..."
                rows={2}
                className="w-64 rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground"
              />
              <button
                onClick={() => resumeMutation.mutate()}
                disabled={resumeMutation.isPending || !guidance.trim()}
                className="rounded-md bg-green-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-green-600 disabled:opacity-50"
              >
                {resumeMutation.isPending ? 'Resuming...' : 'Resume'}
              </button>
              <button
                onClick={() => {
                  setShowResume(false)
                  setGuidance('')
                }}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setShowResume(true)}
              className="rounded-md border border-green-800 px-3 py-1.5 text-sm text-green-400 hover:bg-green-900/30"
            >
              Resume
            </button>
          )}
        </>
      )}
    </div>
  )
}
