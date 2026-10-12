import assert from 'node:assert/strict'
import test from 'node:test'

import { clockTime, humanizeLabel, shortDateTime } from '../src/lib/display.ts'

test('humanizeLabel turns snake_case into words and keeps acronyms', () => {
  assert.equal(humanizeLabel('product_owner'), 'Product Owner')
  assert.equal(humanizeLabel('in_dev'), 'In Dev')
  assert.equal(humanizeLabel('qa'), 'QA')
  assert.equal(humanizeLabel('status_changed'), 'Status Changed')
  assert.equal(humanizeLabel(null), '—')
})

test('shortDateTime is a 24 h month-day stamp, year only when it differs', () => {
  const now = Date.parse('2026-10-12T12:00:00Z')
  assert.match(shortDateTime('2026-10-12T21:03:04Z', now), /^[A-Z][a-z]{2} \d{1,2}, \d{2}:\d{2}$/)
  assert.match(shortDateTime('2025-03-01T10:00:00Z', now), /2025/)
  assert.equal(shortDateTime('nope', now), '—')
  assert.doesNotMatch(shortDateTime('2026-10-12T21:03:04Z', now), /AM|PM|\//)
})

test('clockTime is HH:MM', () => {
  assert.match(clockTime('2026-10-12T21:03:04Z'), /^\d{2}:\d{2}$/)
})
