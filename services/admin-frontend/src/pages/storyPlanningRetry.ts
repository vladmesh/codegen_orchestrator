import type { Story } from '../types/api'

type StoryApi = {
  post: <T>(path: string, body: unknown) => Promise<T>
}

export interface PlanningRetryTarget {
  detail: string
  failedAttempts: number | null
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? value as Record<string, unknown> : null
}

// A story the architect could not plan is parked with a `planning_failed`
// StoryFailure; only that stop is re-run by `retry-planning`.
export function planningRetryTarget(story: Story): PlanningRetryTarget | null {
  if (story.status !== 'waiting_human_review') return null
  const reason = record(story.quarantine_reason)
  if (reason?.reason !== 'story_failure' || reason.code !== 'planning_failed') return null
  return {
    detail: typeof reason.detail === 'string' ? reason.detail : '',
    failedAttempts: story.planning?.failed_attempts ?? null,
  }
}

export function requestPlanningRetry(api: StoryApi, storyId: string) {
  return api.post<Story>(`/stories/${storyId}/retry-planning`, { actor: 'admin' })
}
