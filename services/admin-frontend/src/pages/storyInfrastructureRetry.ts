import type { Story, Task } from '@/types/api'

export const INFRASTRUCTURE_REFUSALS = new Set([
  'executor_unavailable',
  'executor_confirmation_required',
  'project_locked',
  'worker_profile_unavailable',
  'worker_creation_failed',
  'workspace_ensure_failed',
])

export interface InfrastructureRetryTarget {
  taskId: string
  attemptId: string
  refusal: string
  detail: string
}

type ApiPost = {
  post: <T>(path: string, body: unknown) => Promise<T>
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? value as Record<string, unknown> : null
}

export function infrastructureRetryTarget(
  story: Story,
  tasks: Task[],
): InfrastructureRetryTarget | null {
  if (story.status !== 'waiting_human_review') return null
  const wrapper = record(story.quarantine_reason)
  const evidence = record(wrapper?.engineering_infrastructure)
  if (
    evidence?.execution_phase !== 'pre_agent_refused'
    || typeof evidence.refusal !== 'string'
    || !INFRASTRUCTURE_REFUSALS.has(evidence.refusal)
    || typeof evidence.task_id !== 'string'
    || typeof evidence.attempt_id !== 'string'
    || typeof evidence.detail !== 'string'
  ) return null
  const task = tasks.find((item) => item.id === evidence.task_id)
  const taskWrapper = record(task?.failure_metadata)
  const taskEvidence = record(taskWrapper?.engineering_infrastructure)
  if (task?.status !== 'waiting_human_review') return null
  if (!taskEvidence || [
    'execution_phase',
    'refusal',
    'task_id',
    'attempt_id',
    'detail',
  ].some((key) => taskEvidence[key] !== evidence[key])) return null
  return {
    taskId: evidence.task_id,
    attemptId: evidence.attempt_id,
    refusal: evidence.refusal,
    detail: evidence.detail,
  }
}

export async function requestInfrastructureRetry(
  api: ApiPost,
  storyId: string,
  target: InfrastructureRetryTarget,
) {
  return api.post(`/stories/${storyId}/retry-infrastructure-attempt`, {
    task_id: target.taskId,
    attempt_id: target.attemptId,
    refusal: target.refusal,
    actor: 'admin',
  })
}
