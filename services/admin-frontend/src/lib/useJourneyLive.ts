import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import { livePoll } from './live'
import type { JourneyLive } from '@/types/api'

/** One request per story (and task) feeds both the log and the repository panel. */
export function useJourneyLive(storyId: string | undefined, taskId?: string, file?: string | null) {
  const params = new URLSearchParams()
  if (taskId) params.set('task', taskId)
  if (file) params.set('file', file)
  const qs = params.toString()
  return useQuery({
    queryKey: ['journey-live', storyId, taskId ?? null, file ?? null],
    queryFn: () => api.get<JourneyLive>(`/admin/v2/journeys/${storyId}/live${qs ? `?${qs}` : ''}`),
    enabled: Boolean(storyId),
    refetchInterval: livePoll(10_000),
    placeholderData: (previous) => previous,
  })
}
