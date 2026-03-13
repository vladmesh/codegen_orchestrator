import { useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { WorkspaceBrowser } from '@/components/workspace'
import type {
  WorkerDetail,
  WorkerLogsResponse,
  PromptsResponse,
} from '@/types/api'

type Tab = 'console' | 'prompts' | 'files'

export function WorkerDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState<Tab>('console')
  const [confirmKill, setConfirmKill] = useState(false)

  const { data: worker, isLoading } = useQuery({
    queryKey: ['worker', id],
    queryFn: () => api.raw<WorkerDetail>(`/wm-api/workers/${id}`),
    enabled: !!id,
    refetchInterval: 5_000,
  })

  const killMutation = useMutation({
    mutationFn: () => api.rawDelete<void>(`/wm-api/workers/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workers'] })
      navigate('/workers')
    },
  })

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!worker) return <p className="text-muted-foreground">Worker not found</p>

  const tabs: { key: Tab; label: string }[] = [
    { key: 'console', label: 'Console' },
    { key: 'prompts', label: 'Prompts' },
    { key: 'files', label: 'Files' },
  ]

  // Resolve workspace API URLs: prefer project workspace, fall back to worker-level
  const workspaceUrls = worker.project_id
    ? {
        tree: `/wm-api/workspaces/${worker.project_id}/tree`,
        files: `/wm-api/workspaces/${worker.project_id}/files/`,
        key: `workspace-${worker.project_id}`,
      }
    : {
        tree: `/wm-api/workers/${id}/tree`,
        files: `/wm-api/workers/${id}/files/`,
        key: `worker-files-${id}`,
      }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link to="/workers" className="text-muted-foreground hover:text-foreground">
            Workers
          </Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="font-mono text-xl font-bold text-foreground">{worker.id.slice(0, 12)}</h1>
          <StatusBadge status={worker.status.toLowerCase()} />
        </div>
        <div>
          {confirmKill ? (
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Kill this worker?</span>
              <button
                onClick={() => killMutation.mutate()}
                disabled={killMutation.isPending}
                className="rounded-md bg-red-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50"
              >
                {killMutation.isPending ? 'Killing...' : 'Confirm'}
              </button>
              <button
                onClick={() => setConfirmKill(false)}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmKill(true)}
              className="rounded-md border border-red-800 px-3 py-1.5 text-sm text-red-400 hover:bg-red-900/30"
            >
              Kill Worker
            </button>
          )}
        </div>
      </div>

      {/* Worker metadata */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <p className="text-sm text-muted-foreground">Project</p>
          <p className="mt-1 text-foreground">
            {worker.project_id ? (
              <Link to={`/projects/${worker.project_id}`} className="text-primary hover:underline">
                {worker.project_id.slice(0, 8)}
              </Link>
            ) : (
              '-'
            )}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Container</p>
          <p className="mt-1 font-mono text-xs text-foreground">
            {worker.container_id?.slice(0, 12) ?? '-'}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Image</p>
          <p className="mt-1 font-mono text-xs text-foreground">{worker.image ?? '-'}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Network</p>
          <p className="mt-1 font-mono text-xs text-foreground">{worker.dev_network ?? '-'}</p>
        </Card>
      </div>

      {worker.error && (
        <Card className="border-red-800">
          <p className="text-sm font-medium text-red-400">Error</p>
          <pre className="mt-1 whitespace-pre-wrap text-sm text-red-300">{worker.error}</pre>
        </Card>
      )}

      {/* Tabs */}
      <div className="border-b border-border">
        <nav className="flex gap-4">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`border-b-2 px-1 pb-2 text-sm font-medium transition-colors ${
                activeTab === tab.key
                  ? 'border-primary text-primary'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab content */}
      {activeTab === 'console' && <ConsoleTab workerId={id!} />}
      {activeTab === 'prompts' && <PromptsTab workerId={id!} />}
      {activeTab === 'files' && (
        <WorkspaceBrowser
          treeApiUrl={workspaceUrls.tree}
          fileApiUrlPrefix={workspaceUrls.files}
          queryKeyPrefix={workspaceUrls.key}
        />
      )}
    </div>
  )
}

/* ---------- Console Tab ---------- */

function ConsoleTab({ workerId }: { workerId: string }) {
  const [tail, setTail] = useState(200)

  const { data, isLoading } = useQuery({
    queryKey: ['worker-logs', workerId, tail],
    queryFn: () => api.raw<WorkerLogsResponse>(`/wm-api/workers/${workerId}/logs?tail=${tail}`),
    refetchInterval: 5_000,
  })

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <label className="text-sm text-muted-foreground">Tail:</label>
        <select
          value={tail}
          onChange={(e) => setTail(Number(e.target.value))}
          className="rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground"
        >
          {[100, 200, 500, 1000].map((n) => (
            <option key={n} value={n}>
              {n} lines
            </option>
          ))}
        </select>
      </div>
      {isLoading ? (
        <p className="text-muted-foreground">Loading logs...</p>
      ) : (
        <pre className="max-h-[600px] overflow-auto rounded-lg bg-zinc-950 p-4 font-mono text-xs leading-relaxed text-zinc-300">
          {data?.logs || 'No logs available'}
        </pre>
      )}
    </div>
  )
}

/* ---------- Prompts Tab ---------- */

function PromptsTab({ workerId }: { workerId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['worker-prompts', workerId],
    queryFn: () => api.raw<PromptsResponse>(`/wm-api/workers/${workerId}/prompts`),
  })

  if (isLoading) return <p className="text-muted-foreground">Loading prompts...</p>

  return (
    <div className="space-y-4">
      <Card>
        <h3 className="mb-2 text-sm font-medium text-muted-foreground">CLAUDE.md</h3>
        {data?.claude_md ? (
          <pre className="max-h-[400px] overflow-auto whitespace-pre-wrap font-mono text-xs text-foreground">
            {data.claude_md}
          </pre>
        ) : (
          <p className="text-sm text-muted-foreground">Not found in workspace</p>
        )}
      </Card>
      <Card>
        <h3 className="mb-2 text-sm font-medium text-muted-foreground">TASK.md</h3>
        {data?.task_md ? (
          <pre className="max-h-[400px] overflow-auto whitespace-pre-wrap font-mono text-xs text-foreground">
            {data.task_md}
          </pre>
        ) : (
          <p className="text-sm text-muted-foreground">Not found in workspace</p>
        )}
      </Card>
    </div>
  )
}
