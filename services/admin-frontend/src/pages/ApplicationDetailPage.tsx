import { useParams, Link } from 'react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { ConfirmButton } from '@/components/ui/ConfirmButton'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { formatDate } from '@/lib/utils'
import type { Application, ApplicationHealthEntry, Repository } from '@/types/api'

export function ApplicationDetailPage() {
  const { id } = useParams<{ id: string }>()
  const queryClient = useQueryClient()

  const { data: app, isLoading } = useQuery({
    queryKey: ['application', id],
    queryFn: () => api.get<Application>(`/applications/${id}`),
    enabled: !!id,
    refetchInterval: 10_000,
  })

  const { data: repo } = useQuery({
    queryKey: ['repository', app?.repo_id],
    queryFn: () => api.get<Repository>(`/repositories/${app!.repo_id}`),
    enabled: !!app?.repo_id,
  })

  const { data: healthHistory } = useQuery({
    queryKey: ['app-health', id],
    queryFn: () => api.get<ApplicationHealthEntry[]>(`/applications/${id}/health-history?hours=24`),
    enabled: !!id,
  })

  const invalidateApp = () => {
    queryClient.invalidateQueries({ queryKey: ['application', id] })
  }

  const stopMutation = useMutation({
    mutationFn: () => api.post<Application>(`/applications/${id}/stop`, { actor: 'admin' }),
    onSuccess: invalidateApp,
  })

  const undeployMutation = useMutation({
    mutationFn: () => api.post<Application>(`/applications/${id}/undeploy`, { actor: 'admin' }),
    onSuccess: invalidateApp,
  })

  const redeployMutation = useMutation({
    mutationFn: () => api.post<Application>(`/applications/${id}/redeploy`, { actor: 'admin' }),
    onSuccess: invalidateApp,
  })

  const e2eMutation = useMutation({
    mutationFn: () => api.post<unknown>(`/applications/${id}/run-e2e`, { actor: 'admin' }),
    onSuccess: invalidateApp,
  })

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!app) return <p className="text-muted-foreground">Application not found</p>

  const canStop = app.status === 'running'
  const canUndeploy = ['running', 'stopped', 'down', 'degraded'].includes(app.status)
  const canE2E = app.status === 'running'

  const githubUrl = repo?.git_url?.replace(/\.git$/, '')

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {repo && (
            <>
              <Link
                to={`/projects/${repo.project_id}`}
                className="text-muted-foreground hover:text-foreground"
              >
                Project
              </Link>
              <span className="text-muted-foreground">/</span>
            </>
          )}
          <h1 className="text-2xl font-bold text-foreground">{app.service_name}</h1>
          <StatusBadge status={app.status} />
        </div>
        <div className="flex items-center gap-2">
          {canStop && (
            <ConfirmButton
              label="Stop"
              confirmText="Stop this application?"
              pendingLabel="Stopping..."
              onConfirm={() => stopMutation.mutate()}
              isPending={stopMutation.isPending}
              variant="red"
            />
          )}
          {canUndeploy && (
            <ConfirmButton
              label="Undeploy"
              confirmText="Undeploy this application?"
              pendingLabel="Undeploying..."
              onConfirm={() => undeployMutation.mutate()}
              isPending={undeployMutation.isPending}
              variant="red"
            />
          )}
          <ConfirmButton
            label="Redeploy"
            confirmText="Redeploy this application?"
            pendingLabel="Redeploying..."
            onConfirm={() => redeployMutation.mutate()}
            isPending={redeployMutation.isPending}
          />
          {canE2E && (
            <ConfirmButton
              label="Run E2E"
              confirmText="Run E2E tests on this application?"
              pendingLabel="Triggering..."
              onConfirm={() => e2eMutation.mutate()}
              isPending={e2eMutation.isPending}
              variant="green"
            />
          )}
        </div>
      </div>

      {/* Metadata cards */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Card>
          <p className="text-sm text-muted-foreground">Server</p>
          <p className="mt-1 font-mono text-sm text-foreground">{app.server_handle}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Ports</p>
          <p className="mt-1 text-foreground">
            {app.ports.length > 0 ? app.ports.map((p) => p.port).join(', ') : '-'}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Last Health Check</p>
          <p className="mt-1 text-foreground">
            {app.last_health_check ? formatDate(app.last_health_check) : '-'}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Response Time</p>
          <p className="mt-1 text-foreground">
            {app.response_time_ms != null ? `${app.response_time_ms}ms` : '-'}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Uptime (24h)</p>
          <p className="mt-1 text-foreground">
            {app.uptime_pct_24h != null ? `${app.uptime_pct_24h.toFixed(1)}%` : '-'}
          </p>
        </Card>
      </div>

      {/* Extra info */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <p className="text-sm text-muted-foreground">SSL Expires</p>
          <p className="mt-1 text-foreground">
            {app.ssl_expires_at ? formatDate(app.ssl_expires_at) : '-'}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Repository</p>
          <p className="mt-1 text-foreground">
            {repo ? (
              githubUrl ? (
                <a href={githubUrl} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">
                  {repo.name}
                </a>
              ) : (
                repo.name
              )
            ) : (
              app.repo_id
            )}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Created</p>
          <p className="mt-1 text-foreground">{formatDate(app.created_at)}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Updated</p>
          <p className="mt-1 text-foreground">{formatDate(app.updated_at)}</p>
        </Card>
      </div>

      {/* Health history */}
      <Card>
        <h2 className="mb-4 text-sm font-medium text-muted-foreground">
          Health History (last 24h) — {healthHistory?.length ?? 0} entries
        </h2>
        {(healthHistory ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">No health data yet</p>
        ) : (
          <div className="max-h-64 overflow-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="pb-2">Time</th>
                  <th className="pb-2">Status</th>
                  <th className="pb-2">Response</th>
                  <th className="pb-2">SSL Days</th>
                </tr>
              </thead>
              <tbody>
                {(healthHistory ?? []).slice(0, 50).map((entry) => (
                  <tr key={entry.id} className="border-t border-border">
                    <td className="py-1 text-foreground">{formatDate(entry.recorded_at)}</td>
                    <td className="py-1">
                      {entry.metrics.healthy != null && (
                        <span className={entry.metrics.healthy ? 'text-green-400' : 'text-red-400'}>
                          {entry.metrics.healthy ? 'healthy' : 'unhealthy'}
                        </span>
                      )}
                    </td>
                    <td className="py-1 text-foreground">
                      {entry.metrics.response_time_ms != null ? `${entry.metrics.response_time_ms}ms` : '-'}
                    </td>
                    <td className="py-1 text-foreground">
                      {entry.metrics.ssl_days_remaining ?? '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
