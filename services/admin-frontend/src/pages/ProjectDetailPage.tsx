import { useState } from 'react'
import { useParams, Link, useSearchParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { WorkspaceBrowser } from '@/components/workspace'
import { formatDate } from '@/lib/utils'
import type { Application, Project, Repository, Story, Task, User } from '@/types/api'

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

/* ── Repositories section ─────────────────────────────────────── */

function RepositoriesSection({
  repositories,
  applications,
}: {
  repositories: Repository[]
  applications: Application[]
}) {
  if (repositories.length === 0) return null

  const appsByRepo = applications.reduce<Record<string, Application[]>>((acc, app) => {
    ;(acc[app.repo_id] ??= []).push(app)
    return acc
  }, {})

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-foreground">Repositories</h2>
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
                  <div
                    key={app.id}
                    className="flex items-center gap-2 rounded border border-border bg-background p-3 text-sm"
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
                  </div>
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
  stories,
  tasksByStory,
}: {
  stories: Story[]
  tasksByStory: Record<string, Task[]>
}) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-foreground">Stories & Tasks</h2>

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
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 text-left"
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
  repositories,
  applications,
  stories,
  tasksByStory,
}: {
  repositories: Repository[]
  applications: Application[]
  stories: Story[]
  tasksByStory: Record<string, Task[]>
}) {
  return (
    <div className="space-y-6">
      <RepositoriesSection repositories={repositories} applications={applications} />
      <StoriesSection stories={stories} tasksByStory={tasksByStory} />
    </div>
  )
}
