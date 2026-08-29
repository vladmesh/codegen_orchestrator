import assert from 'node:assert/strict'
import test from 'node:test'

import { requestStoryQaRecheck } from '../src/pages/storyRecheck.ts'

test('Story Detail recheck issues its audited request to the API route', async () => {
  const calls: Array<{ path: string, body: unknown }> = []
  const api = {
    post: async <T>(path: string, body: unknown): Promise<T> => {
      calls.push({ path, body })
      return {} as T
    },
  }

  await requestStoryQaRecheck(api, 'story-recheck', 'Server repair verified.')

  assert.deepEqual(calls, [{
    path: '/stories/story-recheck/recheck-qa',
    body: { basis: 'Server repair verified.' },
  }])
})
