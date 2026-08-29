import { useState } from 'react'
import { useParams, Link } from 'react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { ConfirmButton } from '@/components/ui/ConfirmButton'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { formatDate } from '@/lib/utils'
import type { Story, Task } from '@/types/api'
import { requestStoryQaRecheck } from './storyRecheck'

const ARCHITECT_STATUSES = new Set(['created', 'reopened'])

export function StoryDetailPage() {
  const { id } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const [acceptanceBasis, setAcceptanceBasis] = useState('')
  const [recheckBasis, setRecheckBasis] = useState('')

  const { data: story, isLoading } = useQuery({
    queryKey: ['story', id],
    queryFn: () => api.get<Story>(`/stories/${id}`),
    enabled: !!id,
  })

  const { data: tasks } = useQuery({
    queryKey: ['tasks', 'story', id],
    queryFn: () => api.get<Task[]>(`/tasks/?story_id=${id}&limit=200`),
    enabled: !!id,
  })

  const sendToArchitectMutation = useMutation({
    mutationFn: () => api.post<Story>(`/stories/${id}/send-to-architect`, { actor: 'admin' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['story', id] })
    },
  })

  const acceptResultMutation = useMutation({
    mutationFn: (basis: string) => api.post<Story>(`/stories/${id}/accept-result`, { basis }),
    onSuccess: () => {
      setAcceptanceBasis('')
      queryClient.invalidateQueries({ queryKey: ['story', id] })
    },
  })

  const recheckQaMutation = useMutation({
    mutationFn: (basis: string) => requestStoryQaRecheck(api, id!, basis),
    onSuccess: () => {
      setRecheckBasis('')
      queryClient.invalidateQueries({ queryKey: ['story', id] })
    },
  })

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!story) return <p className="text-muted-foreground">Story not found</p>

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link to={`/projects/${story.project_id}`} className="text-muted-foreground hover:text-foreground">
            Project
          </Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="text-2xl font-bold text-foreground">{story.title}</h1>
          <StatusBadge status={story.status} />
        </div>
        <div className="flex items-center gap-2">
          {ARCHITECT_STATUSES.has(story.status) && (
            <ConfirmButton
              label="Send to Architect"
              confirmText="Send this story to the architect?"
              pendingLabel="Sending..."
              onConfirm={() => sendToArchitectMutation.mutate()}
              isPending={sendToArchitectMutation.isPending}
            />
          )}
          {story.status === 'waiting_human_review' && (
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-2">
                <textarea
                  value={recheckBasis}
                  onChange={(event) => setRecheckBasis(event.target.value)}
                  placeholder="Basis for rechecking QA"
                  className="min-h-10 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground"
                />
                <ConfirmButton
                  label="Recheck QA"
                  confirmText="Re-deploy or re-run QA for this quarantined story?"
                  pendingLabel="Rechecking..."
                  disabled={!recheckBasis.trim()}
                  onConfirm={() => recheckQaMutation.mutate(recheckBasis.trim())}
                  isPending={recheckQaMutation.isPending}
                />
              </div>
              <div className="flex items-center gap-2">
                <textarea
                  value={acceptanceBasis}
                  onChange={(event) => setAcceptanceBasis(event.target.value)}
                  placeholder="Basis for accepting this result"
                  className="min-h-10 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground"
                />
                <ConfirmButton
                  label="Accept Result"
                  confirmText="Complete this story and notify its owner?"
                  pendingLabel="Accepting..."
                  variant="green"
                  disabled={!acceptanceBasis.trim()}
                  onConfirm={() => acceptResultMutation.mutate(acceptanceBasis.trim())}
                  isPending={acceptResultMutation.isPending}
                />
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Metadata cards */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Card>
          <p className="text-sm text-muted-foreground">Type</p>
          <p className="mt-1 text-foreground">{story.type}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Priority</p>
          <p className="mt-1 text-foreground">{story.priority}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Created by</p>
          <p className="mt-1 text-foreground">{story.created_by}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Created</p>
          <p className="mt-1 text-foreground">{formatDate(story.created_at)}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Updated</p>
          <p className="mt-1 text-foreground">{story.updated_at ? formatDate(story.updated_at) : '-'}</p>
        </Card>
      </div>

      {/* Description */}
      {story.description && (
        <Card>
          <h2 className="mb-2 text-sm font-medium text-muted-foreground">Description</h2>
          <pre className="whitespace-pre-wrap font-sans text-sm text-foreground">
            {story.description}
          </pre>
        </Card>
      )}

      {/* Acceptance criteria */}
      {story.acceptance_criteria && (
        <Card>
          <h2 className="mb-2 text-sm font-medium text-muted-foreground">Acceptance Criteria</h2>
          <pre className="whitespace-pre-wrap font-sans text-sm text-foreground">
            {story.acceptance_criteria}
          </pre>
        </Card>
      )}

      {/* User report (for reopened stories) */}
      {story.user_report && (
        <Card className="border-yellow-800">
          <h2 className="mb-2 text-sm font-medium text-yellow-400">User Report</h2>
          <pre className="whitespace-pre-wrap font-sans text-sm text-foreground">
            {story.user_report}
          </pre>
        </Card>
      )}

      {story.operator_acceptance && (
        <Card className="border-green-800">
          <h2 className="mb-2 text-sm font-medium text-green-400">Operator acceptance</h2>
          <p className="text-sm text-foreground">{story.operator_acceptance.basis}</p>
          <p className="mt-2 text-xs text-muted-foreground">
            {story.operator_acceptance.actor} at {formatDate(story.operator_acceptance.accepted_at)}
          </p>
        </Card>
      )}

      {story.operator_recheck && (
        <Card className="border-blue-800">
          <h2 className="mb-2 text-sm font-medium text-blue-400">Operator QA recheck</h2>
          <p className="text-sm text-foreground">{story.operator_recheck.basis}</p>
          <p className="mt-2 text-xs text-muted-foreground">
            {story.operator_recheck.actor} at {formatDate(story.operator_recheck.rechecked_at)}
          </p>
        </Card>
      )}

      {/* Tasks */}
      <Card>
        <h2 className="mb-4 text-sm font-medium text-muted-foreground">
          Tasks ({tasks?.length ?? 0})
        </h2>
        {(tasks ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">No tasks yet</p>
        ) : (
          <ul className="space-y-1">
            {(tasks ?? []).map((task) => (
              <li key={task.id} className="flex items-center gap-2 text-sm">
                <StatusBadge status={task.status} />
                <Link to={`/tasks/${task.id}`} className="text-primary hover:underline">
                  {task.title}
                </Link>
                <span className="text-xs text-muted-foreground">{task.type}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
