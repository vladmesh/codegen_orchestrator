import type { Story } from '../types/api'

type StoryApi = {
  post: <T>(path: string, body: unknown) => Promise<T>
}

export function requestStoryQaRecheck(api: StoryApi, storyId: string, basis: string) {
  return api.post<Story>(`/stories/${storyId}/recheck-qa`, { basis })
}
