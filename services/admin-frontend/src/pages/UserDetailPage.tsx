import { useState } from 'react'
import { useParams, Link } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { formatDate, relativeTime, cn } from '@/lib/utils'
import { langfuseUrl } from '@/lib/langfuse'
import type { User, Project } from '@/types/api'

type Tab = 'projects' | 'tracing'

export function UserDetailPage() {
  const { id } = useParams<{ id: string }>()
  const [activeTab, setActiveTab] = useState<Tab>('projects')

  const { data: user, isLoading } = useQuery({
    queryKey: ['user', id],
    queryFn: () => api.get<User>(`/users/${id}`),
    enabled: !!id,
  })

  const { data: projects } = useQuery({
    queryKey: ['projects', 'owner', id],
    queryFn: () => api.get<Project[]>(`/projects/?owner_id=${id}`),
    enabled: !!id,
  })

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!user) return <p className="text-muted-foreground">User not found</p>

  const displayName = user.first_name
    ? `${user.first_name}${user.last_name ? ` ${user.last_name}` : ''}`
    : user.username ?? `User #${user.id}`

  const tabs: { key: Tab; label: string }[] = [
    { key: 'projects', label: `Projects (${projects?.length ?? 0})` },
    { key: 'tracing', label: 'LLM Tracing' },
  ]

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/users" className="text-muted-foreground hover:text-foreground">
          Users
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="text-2xl font-bold text-foreground">{displayName}</h1>
        {user.is_admin && (
          <span className="inline-flex items-center rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-500">
            admin
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <p className="text-sm text-muted-foreground">Telegram ID</p>
          <p className="mt-1 font-mono text-sm text-foreground">{user.telegram_id}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Username</p>
          <p className="mt-1 text-foreground">{user.username ? `@${user.username}` : '—'}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Last seen</p>
          <p className="mt-1 text-foreground">{relativeTime(user.last_seen)}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Registered</p>
          <p className="mt-1 text-foreground">{formatDate(user.created_at)}</p>
        </Card>
      </div>

      {/* Tabs */}
      <div className="border-b border-border">
        <nav className="-mb-px flex gap-4">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={cn(
                'border-b-2 px-1 py-2 text-sm font-medium transition-colors',
                activeTab === tab.key
                  ? 'border-primary text-foreground'
                  : 'border-transparent text-muted-foreground hover:border-border hover:text-foreground',
              )}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab content */}
      {activeTab === 'projects' && (
        <div className="space-y-4">
          {(projects ?? []).length === 0 ? (
            <p className="text-muted-foreground">No projects yet</p>
          ) : (
            <div className="overflow-hidden rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead className="border-b border-border bg-muted/50">
                  <tr>
                    <th className="px-4 py-3 text-left font-medium text-muted-foreground">Name</th>
                    <th className="px-4 py-3 text-left font-medium text-muted-foreground">Status</th>
                    <th className="px-4 py-3 text-left font-medium text-muted-foreground">Domain</th>
                    <th className="px-4 py-3 text-left font-medium text-muted-foreground">Updated</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(projects ?? []).map((project) => (
                    <tr key={project.id} className="hover:bg-muted/30">
                      <td className="px-4 py-3">
                        <Link
                          to={`/projects/${project.id}`}
                          className="font-medium text-primary hover:underline"
                        >
                          {project.name}
                        </Link>
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={project.status} />
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {project.domain ?? '—'}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {relativeTime(project.updated_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {activeTab === 'tracing' && (
        <div className="flex h-[600px] flex-col rounded-lg border border-border overflow-hidden">
          <iframe
            src={langfuseUrl()}
            className="flex-1 w-full border-0"
            title="Langfuse Tracing"
          />
        </div>
      )}
    </div>
  )
}
