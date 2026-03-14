import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '@/lib/api'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { MultiSelect } from '@/components/ui/MultiSelect'
import { relativeTime } from '@/lib/utils'
import type { Task } from '@/types/api'

const STATUSES = [
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

const TYPES = ['feature', 'create', 'fix', 'refactor']

type SortField = 'status' | 'priority' | 'updated_at'
type SortDir = 'asc' | 'desc'

function SortIcon({ field, sortField, sortDir }: { field: SortField; sortField: SortField | null; sortDir: SortDir }) {
  if (sortField !== field) {
    return (
      <svg className="ml-1 inline h-3 w-3 opacity-30" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M7 16V4m0 0L3 8m4-4l4 4m6 0v12m0 0l4-4m-4 4l-4-4" />
      </svg>
    )
  }
  return sortDir === 'asc' ? (
    <svg className="ml-1 inline h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
    </svg>
  ) : (
    <svg className="ml-1 inline h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
    </svg>
  )
}

export function TasksPage() {
  const [statusFilter, setStatusFilter] = useState<string[]>([])
  const [typeFilter, setTypeFilter] = useState<string[]>([])
  const [sortField, setSortField] = useState<SortField | null>(null)
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  const { data: tasks, isLoading } = useQuery({
    queryKey: ['tasks', statusFilter, typeFilter],
    queryFn: () => api.get<Task[]>('/tasks/?limit=200'),
  })

  const filteredAndSorted = useMemo(() => {
    let result = tasks ?? []

    if (statusFilter.length > 0) {
      result = result.filter((t) => statusFilter.includes(t.status))
    }
    if (typeFilter.length > 0) {
      result = result.filter((t) => typeFilter.includes(t.type))
    }

    if (sortField) {
      result = [...result].sort((a, b) => {
        let cmp = 0
        if (sortField === 'status') {
          cmp = a.status.localeCompare(b.status)
        } else if (sortField === 'priority') {
          cmp = a.priority - b.priority
        } else if (sortField === 'updated_at') {
          cmp = new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime()
        }
        return sortDir === 'asc' ? cmp : -cmp
      })
    }

    return result
  }, [tasks, statusFilter, typeFilter, sortField, sortDir])

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortField(field)
      setSortDir('asc')
    }
  }

  const thSortable = 'cursor-pointer select-none hover:text-foreground'
  const thBase = 'px-4 py-3 text-left font-medium text-muted-foreground'

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Tasks</h1>

      <div className="flex gap-4">
        <MultiSelect
          options={STATUSES}
          selected={statusFilter}
          onChange={setStatusFilter}
          placeholder="All statuses"
        />
        <MultiSelect
          options={TYPES}
          selected={typeFilter}
          onChange={setTypeFilter}
          placeholder="All types"
        />
      </div>

      {isLoading ? (
        <p className="text-muted-foreground">Loading...</p>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/50">
              <tr>
                <th className={thBase}>Title</th>
                <th className={`${thBase} ${thSortable}`} onClick={() => handleSort('status')}>
                  Status
                  <SortIcon field="status" sortField={sortField} sortDir={sortDir} />
                </th>
                <th className={thBase}>Type</th>
                <th className={`${thBase} ${thSortable}`} onClick={() => handleSort('priority')}>
                  Priority
                  <SortIcon field="priority" sortField={sortField} sortDir={sortDir} />
                </th>
                <th className={`${thBase} ${thSortable}`} onClick={() => handleSort('updated_at')}>
                  Updated
                  <SortIcon field="updated_at" sortField={sortField} sortDir={sortDir} />
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {filteredAndSorted.map((task) => (
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
