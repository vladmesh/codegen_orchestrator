import { useState, useMemo } from 'react'
import { useParams, Link, useNavigate } from 'react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import type {
  WorkerDetail,
  WorkerLogsResponse,
  PromptsResponse,
  FileTreeEntry,
  FileContentResponse,
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
      {activeTab === 'files' && <FilesTab workerId={id!} />}
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

/* ---------- Files Tab ---------- */

interface TreeNode {
  name: string
  path: string
  is_dir: boolean
  size: number
  children: TreeNode[]
}

function buildTree(entries: FileTreeEntry[]): TreeNode[] {
  const root: TreeNode[] = []
  const map = new Map<string, TreeNode>()

  // Sort: directories first, then by path
  const sorted = [...entries].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    return a.path.localeCompare(b.path)
  })

  for (const entry of sorted) {
    const parts = entry.path.split('/')
    const name = parts[parts.length - 1]
    const node: TreeNode = { name, path: entry.path, is_dir: entry.is_dir, size: entry.size, children: [] }
    map.set(entry.path, node)

    const parentPath = parts.slice(0, -1).join('/')
    const parent = parentPath ? map.get(parentPath) : undefined
    if (parent) {
      parent.children.push(node)
    } else {
      root.push(node)
    }
  }

  return root
}

function FilesTab({ workerId }: { workerId: string }) {
  const [selectedFile, setSelectedFile] = useState<string | null>(null)

  const { data: treeData, isLoading: treeLoading } = useQuery({
    queryKey: ['worker-tree', workerId],
    queryFn: () => api.raw<FileTreeEntry[]>(`/wm-api/workers/${workerId}/tree`),
  })

  const { data: fileContent, isLoading: fileLoading } = useQuery({
    queryKey: ['worker-file', workerId, selectedFile],
    queryFn: () =>
      api.raw<FileContentResponse>(`/wm-api/workers/${workerId}/files/${selectedFile}`),
    enabled: !!selectedFile,
  })

  const tree = useMemo(() => buildTree(treeData ?? []), [treeData])

  if (treeLoading) return <p className="text-muted-foreground">Loading file tree...</p>

  return (
    <div className="flex gap-4" style={{ minHeight: 400 }}>
      {/* Tree panel */}
      <div className="w-72 shrink-0 overflow-auto rounded-lg border border-border p-2">
        {tree.length === 0 ? (
          <p className="text-sm text-muted-foreground">Empty workspace</p>
        ) : (
          tree.map((node) => (
            <TreeNodeView
              key={node.path}
              node={node}
              selectedFile={selectedFile}
              onSelect={setSelectedFile}
              depth={0}
            />
          ))
        )}
      </div>

      {/* File content panel */}
      <div className="flex-1 overflow-auto rounded-lg border border-border">
        {selectedFile ? (
          fileLoading ? (
            <p className="p-4 text-muted-foreground">Loading file...</p>
          ) : fileContent ? (
            <div>
              <div className="border-b border-border px-4 py-2">
                <span className="font-mono text-xs text-muted-foreground">{selectedFile}</span>
                <span className="ml-2 text-xs text-muted-foreground">
                  ({formatSize(fileContent.size)})
                </span>
              </div>
              <pre className="overflow-auto p-4 font-mono text-xs leading-relaxed text-foreground">
                {fileContent.content}
              </pre>
            </div>
          ) : (
            <p className="p-4 text-muted-foreground">Could not load file</p>
          )
        ) : (
          <p className="p-4 text-muted-foreground">Select a file to view its contents</p>
        )}
      </div>
    </div>
  )
}

function TreeNodeView({
  node,
  selectedFile,
  onSelect,
  depth,
}: {
  node: TreeNode
  selectedFile: string | null
  onSelect: (path: string) => void
  depth: number
}) {
  const [expanded, setExpanded] = useState(depth < 1)

  if (node.is_dir) {
    return (
      <div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-sm text-foreground hover:bg-muted/50"
          style={{ paddingLeft: depth * 12 + 4 }}
        >
          <span className="text-xs text-muted-foreground">{expanded ? '▼' : '▶'}</span>
          <span>{node.name}/</span>
        </button>
        {expanded &&
          node.children.map((child) => (
            <TreeNodeView
              key={child.path}
              node={child}
              selectedFile={selectedFile}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
      </div>
    )
  }

  return (
    <button
      onClick={() => onSelect(node.path)}
      className={`flex w-full items-center rounded px-1 py-0.5 text-left text-sm ${
        selectedFile === node.path
          ? 'bg-primary/20 text-primary'
          : 'text-foreground hover:bg-muted/50'
      }`}
      style={{ paddingLeft: depth * 12 + 4 }}
    >
      {node.name}
    </button>
  )
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
