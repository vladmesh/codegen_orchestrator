import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { requiresPaidWorkControlConfirmation } from '../src/pages/paidWorkControlTransition.ts'

test('every executor override transition requires confirmation, including reset', () => {
  assert.equal(requiresPaidWorkControlConfirmation('engineering_executor_override'), true)
  assert.equal(requiresPaidWorkControlConfirmation('qa_executor_override'), true)
  assert.equal(requiresPaidWorkControlConfirmation('emergency_stop'), true)
  assert.equal(requiresPaidWorkControlConfirmation('max_concurrent_paid_runs'), false)
})

test('Settings delegates paid-work confirmation to the transition seam', () => {
  const settings = readFileSync(
    new URL('../src/pages/SettingsPage.tsx', import.meta.url),
    'utf8',
  )

  assert.match(settings, /requiresPaidWorkControlConfirmation\(field\)/)
})
