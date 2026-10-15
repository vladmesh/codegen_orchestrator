import assert from 'node:assert/strict'
import test from 'node:test'

import { formatDate, parseApiTime, relativeTime } from '../src/lib/utils.ts'

const NOW = Date.parse('2026-08-27T21:34:33.103Z')
const NINETY_SECONDS_AGO = '2026-08-27T21:33:03.102772'
const ZONES = ['UTC', 'Europe/Berlin', 'America/New_York']

for (const zone of ZONES) {
  test(`a timezone-less UTC timestamp 90 s old reads 1m ago in ${zone}`, (t) => {
    process.env.TZ = zone
    t.mock.method(Date, 'now', () => NOW)
    if (zone !== 'UTC') {
      assert.notEqual(new Date(NINETY_SECONDS_AGO).getTime(), parseApiTime(NINETY_SECONDS_AGO).getTime(), `zone ${zone} not applied`)
    }

    assert.equal(relativeTime(NINETY_SECONDS_AGO), '1m ago')
  })

  test(`Z, +00:00 and no designator name the same instant in ${zone}`, (t) => {
    process.env.TZ = zone
    t.mock.method(Date, 'now', () => NOW)

    assert.equal(relativeTime(`${NINETY_SECONDS_AGO}Z`), '1m ago')
    assert.equal(relativeTime(`${NINETY_SECONDS_AGO}+00:00`), '1m ago')
    assert.equal(parseApiTime(`${NINETY_SECONDS_AGO}+00:00`).getTime(), parseApiTime(NINETY_SECONDS_AGO).getTime())
  })

  test(`formatDate of a timezone-less value equals formatDate of it with Z in ${zone}`, () => {
    process.env.TZ = zone

    assert.equal(formatDate(NINETY_SECONDS_AGO), formatDate(`${NINETY_SECONDS_AGO}Z`))
  })
}

test('an explicit non-UTC offset is kept', () => {
  assert.equal(parseApiTime('2026-08-27T23:33:03+02:00').toISOString(), '2026-08-27T21:33:03.000Z')
})
