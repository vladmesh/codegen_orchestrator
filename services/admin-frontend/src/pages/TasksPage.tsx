import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '@/lib/api'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { relativeTime } from '@/lib/utils'
import type { Task } from '@/types/api'

const STATUSES = [
  'all',
  'backlog',
  'todo',
  'in_dev',
  'in_ci',
  'testing',
  'done',
  'blocked',
  'waiting_human_review',
  'failed',
  'cancelled',
]

const TYPES = ['all', 'feature', 'create', 'fix', 'refactor']

export function TasksPage() {
  const [statusFilter, setStatusFilter] = useState('all')
  const [typeFilter, setTypeFilter] = useState('all')

  const queryParams = new URLSearchParams({ limit: '200' })
  if (statusFilter !== 'all') queryParams.set('status', statusFilter)
  if (typeFilter !== 'all') queryParams.set('type', typeFilter)

  const { data: tasks, isLoading } = useQuery({
    queryKey: ['tasks', statusFilter, typeFilter],
    queryFn: () => api.get<Task[]>(`/tasks/?${queryParams.toString()}`),
  })

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Tasks</h1>

      <div className="flex gap-4">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground"
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s === 'all' ? 'All statuses' : s.replace(/_/g, ' ')}
            </option>
          ))}
        </select>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground"
        >
          {TYPES.map((t) => (
            <option key={t} value={t}>
              {t === 'all' ? 'All types' : t}
            </option>
          ))}
        </select>
      </div>

      {isLoading ? (
        <p className="text-muted-foreground">Loading...</p>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/50">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Title</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Status</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Type</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Priority</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(tasks ?? []).map((task) => (
                <tr key={task.id} className="hover:bg-muted/30">
                  <td className="max-w-md truncate px-4 py-3">
                    <Link
                      to={`/tasks/${task.id}`}
                      className="font-medium text-primary hover:underline"
                    >
                      {task.title}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={task.status} />
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">{task.type}</td>
                  <td className="px-4 py-3 text-muted-foreground">{task.priority}</td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {relativeTime(task.updated_at)}
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
