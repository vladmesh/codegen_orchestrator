import assert from 'node:assert/strict'
import test from 'node:test'

import { api } from '../src/lib/api.ts'

test('rawDelete accepts a successful 204 response', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(null, { status: 204 })

  try {
    assert.equal(await api.rawDelete<void>('/wm-api/workers/worker-1'), undefined)
  } finally {
    globalThis.fetch = originalFetch
  }
})
