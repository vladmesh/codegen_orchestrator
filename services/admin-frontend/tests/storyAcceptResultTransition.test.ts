import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('Story Detail sends the required acceptance basis to the audited result route', () => {
  const storyDetail = readFileSync(
    new URL('../src/pages/StoryDetailPage.tsx', import.meta.url),
    'utf8',
  )

  assert.match(
    storyDetail,
    /api\.post<Story>\(`\/stories\/\$\{id\}\/accept-result`, \{ basis \}\)/,
  )
  assert.match(storyDetail, /disabled=\{!acceptanceBasis\.trim\(\)\}/)
  assert.match(storyDetail, /acceptResultMutation\.mutate\(acceptanceBasis\.trim\(\)\)/)
})
