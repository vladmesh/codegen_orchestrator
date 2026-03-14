import { useParams, Link, useSearchParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { WorkspaceBrowser } from '@/components/workspace'
import { formatDate } from '@/lib/utils'
import type { Project, Repository, Story, Task, User } from '@/types/api'

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

  const primaryRepo = repositories?.[0]

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

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/projects" className="text-muted-foreground hover:text-foreground">
          Projects
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="text-2xl font-bold text-foreground">{project.name}</h1>
        <StatusBadge status={project.status} />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <p className="text-sm text-muted-foreground">Domain</p>
          <p className="mt-1 text-foreground">{project.domain ?? '—'}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">GitHub</p>
          <p className="mt-1 truncate text-foreground">{project.github_repo ?? '—'}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Stories</p>
          <p className="mt-1 text-2xl font-semibold text-foreground">{stories?.length ?? 0}</p>
        </Card>
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
          <p className="text-sm text-muted-foreground">Created</p>
          <p className="mt-1 text-foreground">{formatDate(project.created_at)}</p>
        </Card>
      </div>

      {project.description && (
        <Card>
          <p className="text-sm text-muted-foreground">Description</p>
          <p className="mt-2 whitespace-pre-wrap text-foreground">{project.description}</p>
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
      {activeTab === 'overview' && (
        <OverviewTab stories={stories ?? []} tasksByStory={tasksByStory} />
      )}
      {activeTab === 'workspace' && (
        primaryRepo ? (
          <WorkspaceBrowser
            treeApiUrl={`/wm-api/workspaces/${primaryRepo.id}/tree`}
            fileApiUrlPrefix={`/wm-api/workspaces/${primaryRepo.id}/files/`}
            queryKeyPrefix={`workspace-${primaryRepo.id}`}
          />
        ) : (
          <p className="text-muted-foreground">No repository found for this project.</p>
        )
      )}
    </div>
  )
}

function OverviewTab({
  stories,
  tasksByStory,
}: {
  stories: Story[]
  tasksByStory: Record<string, Task[]>
}) {
  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold text-foreground">Stories & Tasks</h2>
      {stories.map((story) => (
        <Card key={story.id}>
          <div className="flex items-center gap-2">
            <h3 className="font-medium text-foreground">{story.title}</h3>
            <StatusBadge status={story.status} />
          </div>
          {(tasksByStory[story.id] ?? []).length > 0 && (
            <ul className="mt-3 space-y-1">
              {(tasksByStory[story.id] ?? []).map((task) => (
                <li key={task.id} className="flex items-center gap-2 text-sm">
                  <StatusBadge status={task.status} />
                  <Link
                    to={`/tasks/${task.id}`}
                    className="text-primary hover:underline"
                  >
                    {task.title}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      ))}

      {(tasksByStory['unlinked'] ?? []).length > 0 && (
        <Card>
          <h3 className="font-medium text-muted-foreground">Unlinked Tasks</h3>
          <ul className="mt-3 space-y-1">
            {tasksByStory['unlinked'].map((task) => (
              <li key={task.id} className="flex items-center gap-2 text-sm">
                <StatusBadge status={task.status} />
                <Link to={`/tasks/${task.id}`} className="text-primary hover:underline">
                  {task.title}
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}
