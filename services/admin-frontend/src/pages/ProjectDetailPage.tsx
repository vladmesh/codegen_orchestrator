import { useParams, Link } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { formatDate } from '@/lib/utils'
import type { Project, Story, Task, User } from '@/types/api'

export function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>()

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

  if (projectLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!project) return <p className="text-muted-foreground">Project not found</p>

  const tasksByStory = (tasks ?? []).reduce<Record<string, Task[]>>((acc, t) => {
    const key = t.story_id ?? 'unlinked'
    ;(acc[key] ??= []).push(t)
    return acc
  }, {})

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

      <div className="space-y-4">
        <h2 className="text-lg font-semibold text-foreground">Stories & Tasks</h2>
        {(stories ?? []).map((story) => (
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

    </div>
  )
}
