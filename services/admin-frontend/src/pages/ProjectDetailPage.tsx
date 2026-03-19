import { useState } from 'react'
import { useParams, Link, useSearchParams } from 'react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { ConfirmButton } from '@/components/ui/ConfirmButton'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { WorkspaceBrowser } from '@/components/workspace'
import { formatDate } from '@/lib/utils'
import type { Application, Project, Repository, Server, Story, Task, User } from '@/types/api'

type Tab = 'overview' | 'workspace'

export function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab = (searchParams.get('tab') as Tab) || 'overview'
  const setActiveTab = (tab: Tab) => setSearchParams({ tab }, { replace: true })

  const { data: project, isLoading: projectLoading } = useQuery({
    queryKey: ['project', id],
    queryFn: () => api.get<Project>(`/projects/${id}`),
    enabled: !!id,
  })

  const { data: owner } = useQuery({
    queryKey: ['user', project?.owner_id],
    queryFn: () => api.get<User>(`/users/${project!.owner_id}`),
    enabled: !!project?.owner_id,
  })

  const { data: stories } = useQuery({
    queryKey: ['stories', id],
    queryFn: () => api.get<Story[]>(`/stories/?project_id=${id}`),
    enabled: !!id,
  })

  const { data: tasks } = useQuery({
    queryKey: ['tasks', 'project', id],
    queryFn: () => api.get<Task[]>(`/tasks/?project_id=${id}&limit=200`),
    enabled: !!id,
  })

  const { data: repositories } = useQuery({
    queryKey: ['repositories', id],
    queryFn: () => api.get<Repository[]>(`/repositories/?project_id=${id}`),
    enabled: !!id,
  })

  const repoIds = repositories?.map((r) => r.id) ?? []

  const { data: applications } = useQuery({
    queryKey: ['applications', 'project', id, repoIds],
    queryFn: async () => {
      const results = await Promise.all(
        repoIds.map((repoId) => api.get<Application[]>(`/applications/?repo_id=${repoId}`)),
      )
      return results.flat()
    },
    enabled: repoIds.length > 0,
  })

  const primaryRepo = repositories?.find((r) => r.role === 'primary') ?? repositories?.[0]

  if (projectLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!project) return <p className="text-muted-foreground">Project not found</p>

  const tasksByStory = (tasks ?? []).reduce<Record<string, Task[]>>((acc, t) => {
    const key = t.story_id ?? 'unlinked'
    ;(acc[key] ??= []).push(t)
    return acc
  }, {})

  const tabs: { key: Tab; label: string }[] = [
    { key: 'overview', label: 'Overview' },
    { key: 'workspace', label: 'Workspace' },
  ]

  // Extract useful info from config
  const githubOrg = project.config?.github_org as string | undefined
  const domain = project.config?.domain as string | undefined

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Link to="/projects" className="text-muted-foreground hover:text-foreground">
          Projects
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="text-2xl font-bold text-foreground">{project.name}</h1>
        <StatusBadge status={project.status} />
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Card>
          <p className="text-sm text-muted-foreground">Owner</p>
          <p className="mt-1 text-foreground">
            {owner ? (
              <Link to={`/users/${owner.id}`} className="text-primary hover:underline">
                {owner.first_name ?? owner.username ?? `User #${owner.id}`}
              </Link>
            ) : (
              '—'
            )}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Stories</p>
          <p className="mt-1 text-2xl font-semibold text-foreground">{stories?.length ?? 0}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Tasks</p>
          <p className="mt-1 text-2xl font-semibold text-foreground">{tasks?.length ?? 0}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Repositories</p>
          <p className="mt-1 text-2xl font-semibold text-foreground">
            {repositories?.length ?? 0}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Created</p>
          <p className="mt-1 text-foreground">{formatDate(project.created_at)}</p>
        </Card>
      </div>

      {/* Extra info row */}
      {(githubOrg || domain) && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {githubOrg && (
            <Card>
              <p className="text-sm text-muted-foreground">GitHub Org</p>
              <p className="mt-1 text-foreground">{githubOrg}</p>
            </Card>
          )}
          {domain && (
            <Card>
              <p className="text-sm text-muted-foreground">Domain</p>
              <p className="mt-1 text-foreground">{domain}</p>
            </Card>
          )}
        </div>
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
      {activeTab === 'overview' && (
        <OverviewTab
          projectId={id!}
          repositories={repositories ?? []}
          applications={applications ?? []}
          stories={stories ?? []}
          tasksByStory={tasksByStory}
        />
      )}
      {activeTab === 'workspace' &&
        (primaryRepo ? (
          <WorkspaceBrowser
            treeApiUrl={`/wm-api/workspaces/${primaryRepo.id}/tree`}
            fileApiUrlPrefix={`/wm-api/workspaces/${primaryRepo.id}/files/`}
            queryKeyPrefix={`workspace-${primaryRepo.id}`}
          />
        ) : (
          <p className="text-muted-foreground">No repository found for this project.</p>
        ))}
    </div>
  )
}

/* ── Secrets Editor ────────────────────────────────────────────── */

function SecretsEditor({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient()
  const [showAdd, setShowAdd] = useState(false)
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')

  const { data: secretKeys } = useQuery({
    queryKey: ['secrets', projectId],
    queryFn: () => api.get<{ keys: string[] }>(`/projects/${projectId}/config/secrets/keys`),
  })

  const addMutation = useMutation({
    mutationFn: () =>
      api.post<{ keys: string[] }>(`/projects/${projectId}/config/secrets`, {
        secrets: { [newKey]: newValue },
      }),
    onSuccess: () => {
      setShowAdd(false)
      setNewKey('')
      setNewValue('')
      queryClient.invalidateQueries({ queryKey: ['secrets', projectId] })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (key: string) =>
      api.delete<{ keys: string[] }>(`/projects/${projectId}/config/secrets/${key}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['secrets', projectId] })
    },
  })

  const keys = secretKeys?.keys ?? []

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-foreground">Secrets</h2>
        <button
          onClick={() => setShowAdd(!showAdd)}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          {showAdd ? 'Cancel' : 'Add Secret'}
        </button>
      </div>

      {showAdd && (
        <Card>
          <div className="flex items-end gap-3">
            <div className="flex-1">
              <label className="mb-1 block text-xs text-muted-foreground">Key</label>
              <input
                type="text"
                value={newKey}
                onChange={(e) => setNewKey(e.target.value)}
                placeholder="SECRET_KEY"
                className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground"
              />
            </div>
            <div className="flex-1">
              <label className="mb-1 block text-xs text-muted-foreground">Value</label>
              <input
                type="password"
                value={newValue}
                onChange={(e) => setNewValue(e.target.value)}
                placeholder="secret_value"
                className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground"
              />
            </div>
            <button
              onClick={() => addMutation.mutate()}
              disabled={addMutation.isPending || !newKey.trim() || !newValue.trim()}
              className="rounded-md bg-blue-700 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50"
            >
              {addMutation.isPending ? 'Saving...' : 'Save'}
            </button>
          </div>
        </Card>
      )}

      {keys.length === 0 ? (
        <p className="text-sm text-muted-foreground">No secrets configured.</p>
      ) : (
        <Card>
          <ul className="space-y-2">
            {keys.map((key) => (
              <li key={key} className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-3">
                  <span className="font-mono text-foreground">{key}</span>
                  <span className="text-muted-foreground">{'••••••••'}</span>
                </div>
                <ConfirmButton
                  label="Delete"
                  confirmText={`Delete ${key}?`}
                  pendingLabel="Deleting..."
                  onConfirm={() => deleteMutation.mutate(key)}
                  isPending={deleteMutation.isPending}
                  variant="red"
                />
              </li>
            ))}
          </ul>
        </Card>
      )}
    </section>
  )
}

/* ── Create Story Form ─────────────────────────────────────────── */

function CreateStoryForm({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [storyType, setStoryType] = useState<'product' | 'technical'>('product')

  const createMutation = useMutation({
    mutationFn: () =>
      api.post<Story>('/stories/', {
        project_id: projectId,
        title,
        description: description || null,
        type: storyType,
        created_by: 'admin',
      }),
    onSuccess: () => {
      setShowForm(false)
      setTitle('')
      setDescription('')
      setStoryType('product')
      queryClient.invalidateQueries({ queryKey: ['stories', projectId] })
    },
  })

  if (!showForm) {
    return (
      <button
        onClick={() => setShowForm(true)}
        className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        Create Story
      </button>
    )
  }

  return (
    <Card>
      <h3 className="mb-3 text-sm font-medium text-foreground">New Story</h3>
      <div className="space-y-3">
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Title</label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Story title"
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Description</label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional description"
            rows={3}
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Type</label>
          <select
            value={storyType}
            onChange={(e) => setStoryType(e.target.value as 'product' | 'technical')}
            className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground"
          >
            <option value="product">Product</option>
            <option value="technical">Technical</option>
          </select>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending || !title.trim()}
            className="rounded-md bg-blue-700 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50"
          >
            {createMutation.isPending ? 'Creating...' : 'Create'}
          </button>
          <button
            onClick={() => {
              setShowForm(false)
              setTitle('')
              setDescription('')
            }}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
          >
            Cancel
          </button>
        </div>
      </div>
    </Card>
  )
}

/* ── Deploy from Repo Form ─────────────────────────────────────── */

function DeployFromRepoForm({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [repoUrl, setRepoUrl] = useState('')
  const [serverHandle, setServerHandle] = useState('')
  const [serviceName, setServiceName] = useState('')

  const { data: servers } = useQuery({
    queryKey: ['servers'],
    queryFn: () => api.get<Server[]>('/servers/'),
    enabled: showForm,
  })

  const deployMutation = useMutation({
    mutationFn: () =>
      api.post<unknown>('/applications/from-repo', {
        repo_url: repoUrl,
        project_id: projectId,
        server_handle: serverHandle,
        service_name: serviceName,
      }),
    onSuccess: () => {
      setShowForm(false)
      setRepoUrl('')
      setServerHandle('')
      setServiceName('')
      queryClient.invalidateQueries({ queryKey: ['applications', 'project', projectId] })
      queryClient.invalidateQueries({ queryKey: ['repositories', projectId] })
    },
  })

  if (!showForm) {
    return (
      <button
        onClick={() => setShowForm(true)}
        className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        Deploy from Repo
      </button>
    )
  }

  return (
    <Card>
      <h3 className="mb-3 text-sm font-medium text-foreground">Deploy from Repository</h3>
      <div className="space-y-3">
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Repository URL</label>
          <input
            type="text"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/org/repo.git"
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Server</label>
          <select
            value={serverHandle}
            onChange={(e) => setServerHandle(e.target.value)}
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground"
          >
            <option value="">Select server...</option>
            {(servers ?? []).map((s) => (
              <option key={s.handle} value={s.handle}>
                {s.handle} ({s.public_ip})
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Service Name</label>
          <input
            type="text"
            value={serviceName}
            onChange={(e) => setServiceName(e.target.value)}
            placeholder="my-service"
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground"
          />
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => deployMutation.mutate()}
            disabled={
              deployMutation.isPending ||
              !repoUrl.trim() ||
              !serverHandle ||
              !serviceName.trim()
            }
            className="rounded-md bg-blue-700 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50"
          >
            {deployMutation.isPending ? 'Deploying...' : 'Deploy'}
          </button>
          <button
            onClick={() => {
              setShowForm(false)
              setRepoUrl('')
              setServerHandle('')
              setServiceName('')
            }}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
          >
            Cancel
          </button>
        </div>
      </div>
    </Card>
  )
}

/* ── Repositories section ─────────────────────────────────────── */

function RepositoriesSection({
  projectId,
  repositories,
  applications,
}: {
  projectId: string
  repositories: Repository[]
  applications: Application[]
}) {
  const appsByRepo = applications.reduce<Record<string, Application[]>>((acc, app) => {
    ;(acc[app.repo_id] ??= []).push(app)
    return acc
  }, {})

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-foreground">Repositories</h2>
        <DeployFromRepoForm projectId={projectId} />
      </div>

      {repositories.length === 0 && (
        <p className="text-sm text-muted-foreground">No repositories yet.</p>
      )}

      {repositories.map((repo) => {
        const repoApps = appsByRepo[repo.id] ?? []
        const githubUrl = repo.git_url?.replace(/\.git$/, '')
        return (
          <Card key={repo.id}>
            <div className="flex items-center gap-2">
              <span className="font-medium text-foreground">{repo.name}</span>
              <StatusBadge status={repo.role} />
              <span className="text-xs text-muted-foreground">{repo.visibility}</span>
              {githubUrl && (
                <a
                  href={githubUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="ml-auto text-sm text-primary hover:underline"
                >
                  GitHub
                </a>
              )}
            </div>

            {/* Applications for this repo */}
            {repoApps.length > 0 && (
              <div className="mt-3 space-y-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Applications
                </p>
                {repoApps.map((app) => (
                  <Link
                    key={app.id}
                    to={`/applications/${app.id}`}
                    className="flex items-center gap-2 rounded border border-border bg-background p-3 text-sm hover:border-primary/50"
                  >
                    <span className="font-medium text-foreground">{app.service_name}</span>
                    <StatusBadge status={app.status} />
                    <span className="text-xs text-muted-foreground">
                      server: {app.server_handle}
                    </span>
                    {app.ports.length > 0 && (
                      <span className="text-xs text-muted-foreground">
                        ports: {app.ports.map((p) => p.port).join(', ')}
                      </span>
                    )}
                    {app.last_health_check && (
                      <span className="ml-auto text-xs text-muted-foreground">
                        health: {formatDate(app.last_health_check)}
                      </span>
                    )}
                  </Link>
                ))}
              </div>
            )}
          </Card>
        )
      })}
    </section>
  )
}

/* ── Stories & Tasks section ────────────────────────────────────── */

function StoriesSection({
  projectId,
  stories,
  tasksByStory,
}: {
  projectId: string
  stories: Story[]
  tasksByStory: Record<string, Task[]>
}) {
  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-foreground">Stories & Tasks</h2>
        <CreateStoryForm projectId={projectId} />
      </div>

      {stories.map((story) => (
        <StoryCard key={story.id} story={story} tasks={tasksByStory[story.id] ?? []} />
      ))}

      {(tasksByStory['unlinked'] ?? []).length > 0 && (
        <Card>
          <h3 className="font-medium text-muted-foreground">Unlinked Tasks</h3>
          <TaskList tasks={tasksByStory['unlinked']} />
        </Card>
      )}

      {stories.length === 0 && (tasksByStory['unlinked'] ?? []).length === 0 && (
        <p className="text-sm text-muted-foreground">No stories or tasks yet.</p>
      )}
    </section>
  )
}

function StoryCard({ story, tasks }: { story: Story; tasks: Task[] }) {
  const [expanded, setExpanded] = useState(false)
  const taskCount = tasks.length
  const doneCount = tasks.filter((t) => t.status === 'done').length

  return (
    <Card>
      <div className="flex w-full items-center gap-2">
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex flex-1 items-center gap-2 text-left"
        >
          <span className="text-muted-foreground">{expanded ? '▾' : '▸'}</span>
          <h3 className="font-medium text-foreground">{story.title}</h3>
          <StatusBadge status={story.status} />
          <span className="text-xs text-muted-foreground">{story.type}</span>
          {taskCount > 0 && (
            <span className="ml-auto text-xs text-muted-foreground">
              {doneCount}/{taskCount} tasks
            </span>
          )}
        </button>
        <Link
          to={`/stories/${story.id}`}
          className="shrink-0 text-xs text-primary hover:underline"
        >
          Details
        </Link>
      </div>

      {expanded && (
        <div className="mt-3 space-y-2">
          {story.description && (
            <p className="whitespace-pre-wrap text-sm text-muted-foreground">
              {story.description}
            </p>
          )}
          {taskCount > 0 ? (
            <TaskList tasks={tasks} />
          ) : (
            <p className="text-xs text-muted-foreground">No tasks</p>
          )}
        </div>
      )}
    </Card>
  )
}

function TaskList({ tasks }: { tasks: Task[] }) {
  return (
    <ul className="mt-2 space-y-1">
      {tasks.map((task) => (
        <li key={task.id} className="flex items-center gap-2 text-sm">
          <StatusBadge status={task.status} />
          <Link to={`/tasks/${task.id}`} className="text-primary hover:underline">
            {task.title}
          </Link>
          <span className="text-xs text-muted-foreground">{task.type}</span>
          {task.elapsed_minutes > 0 && (
            <span className="text-xs text-muted-foreground">{task.elapsed_minutes}m</span>
          )}
        </li>
      ))}
    </ul>
  )
}

/* ── Overview tab ──────────────────────────────────────────────── */

function OverviewTab({
  projectId,
  repositories,
  applications,
  stories,
  tasksByStory,
}: {
  projectId: string
  repositories: Repository[]
  applications: Application[]
  stories: Story[]
  tasksByStory: Record<string, Task[]>
}) {
  return (
    <div className="space-y-6">
      <SecretsEditor projectId={projectId} />
      <RepositoriesSection projectId={projectId} repositories={repositories} applications={applications} />
      <StoriesSection projectId={projectId} stories={stories} tasksByStory={tasksByStory} />
    </div>
  )
}
