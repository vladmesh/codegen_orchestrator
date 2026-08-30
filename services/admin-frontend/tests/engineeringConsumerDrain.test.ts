import assert from 'node:assert/strict'
import test from 'node:test'

import { requestEngineeringConsumerDrain, requestEngineeringConsumerResume } from '../src/pages/engineeringConsumerDrain.ts'

test('operator drain control issues the audited API requests', async () => {
  const calls: Array<{ method: string, path: string, body?: unknown }> = []
  const api = {
    post: async <T>(path: string, body: unknown): Promise<T> => {
      calls.push({ method: 'POST', path, body })
      return {} as T
    },
    delete: async <T>(path: string): Promise<T> => {
      calls.push({ method: 'DELETE', path })
      return {} as T
    },
  }

  await requestEngineeringConsumerDrain(api)
  await requestEngineeringConsumerResume(api)

  assert.deepEqual(calls, [
    { method: 'POST', path: '/engineering-consumer/drain', body: {} },
    { method: 'DELETE', path: '/engineering-consumer/drain' },
  ])
})
