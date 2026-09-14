import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import type { ExecutorDiagnostic, ExecutorProfileObservation } from '../src/types/api.ts'
import { executorProfileAttention, executorProfileFacts } from '../src/pages/executorProfileHealth.ts'

const NOW = new Date('2026-09-14T12:00:00Z')

function diagnostic(overrides: Partial<ExecutorDiagnostic>): ExecutorDiagnostic {
  return {
    executor: 'codex',
    enabled: true,
    auth_mode: 'host_session',
    availability: 'available',
    observed_at: '2026-09-14T12:00:00Z',
    expires_at: '2026-09-14T12:01:30Z',
    active_lease_count: 0,
    reason_code: 'ready',
    reason: 'Local authentication and worker inventory are ready.',
    profile: { condition: 'healthy', login_state: 'logged_in', refresh_material: 'present' },
    ...overrides,
  }
}

function facts(profile: ExecutorProfileObservation): Record<string, string> {
  return Object.fromEntries(executorProfileFacts(profile, NOW).map((fact) => [fact.label, fact.value]))
}

test('access/session expiry and refresh expiry are separate facts', () => {
  const rendered = facts({
    condition: 'healthy',
    login_state: 'logged_in',
    refresh_material: 'present',
    session_expires_at: '2026-09-23T12:00:00+00:00',
    session_expiry_source: 'codex_access_token_jwt_exp',
    last_refresh_at: '2026-09-13T08:30:00.123456+00:00',
    last_refresh_source: 'codex_auth_last_refresh',
  })

  assert.equal(rendered['Login'], 'Logged in')
  assert.equal(rendered['Refresh credential'], 'Present')
  assert.equal(rendered['Access/session expiry'], '2026-09-23T12:00:00Z (access-token exp claim)')
  assert.equal(rendered['Refresh credential expiry'], 'Not exposed by CLI')
  assert.equal(rendered['Last refresh'], '2026-09-13T08:30:00.123Z')
})

test('an expired Claude access token with refresh material is shown as renewable', () => {
  const rendered = facts({
    condition: 'healthy',
    login_state: 'logged_in',
    refresh_material: 'present',
    session_expires_at: '2026-09-14T11:00:00+00:00',
    session_expiry_source: 'claude_oauth_expires_at',
  })

  assert.equal(rendered['Access/session expiry'], '2026-09-14T11:00:00Z (stored access-token expiry; expired, renewable)')
  assert.equal(rendered['Last refresh'], 'Not stored')
})

test('near-expiry is degraded and logged-out or expired is unavailable', () => {
  assert.equal(executorProfileAttention(diagnostic({})), null)
  assert.match(
    executorProfileAttention(diagnostic({
      availability: 'degraded',
      reason_code: 'profile_refresh_expiring',
      reason: 'Host-session refresh credential expires within 24 hours.',
      profile: {
        condition: 'refresh_expiring',
        login_state: 'logged_in',
        refresh_material: 'present',
        refresh_expires_at: '2026-09-14T17:00:00+00:00',
        refresh_expiry_source: 'codex_refresh_token_jwt_exp',
      },
    })) ?? '',
    /^Degraded: Host-session refresh credential expires within 24 hours\./,
  )
  assert.match(
    executorProfileAttention(diagnostic({
      availability: 'unavailable',
      reason_code: 'profile_logged_out',
      reason: 'Host-session profile is logged out.',
      profile: { condition: 'logged_out', login_state: 'logged_out', refresh_material: 'missing' },
    })) ?? '',
    /^Unavailable: Host-session profile is logged out\./,
  )
  assert.match(
    executorProfileAttention(diagnostic({
      availability: 'unavailable',
      reason_code: 'profile_refresh_expired',
      reason: 'Host-session refresh credential has expired.',
      profile: {
        condition: 'refresh_expired',
        login_state: 'expired',
        refresh_material: 'present',
        refresh_expires_at: '2026-09-14T11:00:00+00:00',
        refresh_expiry_source: 'codex_refresh_token_jwt_exp',
      },
    })) ?? '',
    /^Unavailable: /,
  )
  // A stale/fallback snapshot has no profile and stays the existing unknown notice.
  assert.equal(executorProfileAttention(diagnostic({ availability: 'unknown', profile: null })), null)
})

test('Settings renders profile facts and attention from the safe diagnostic only', () => {
  const settings = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8')

  assert.match(settings, /executorProfileFacts\(item\.profile, new Date\(\)\)/)
  assert.match(settings, /executorProfileAttention\(item\)/)
  assert.match(settings, /Diagnostic snapshot is stale\. New paid starts will require confirmation\./)
})
