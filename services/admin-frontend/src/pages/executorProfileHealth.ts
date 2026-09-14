import type {
  CredentialExpirySource,
  ExecutorDiagnostic,
  ExecutorProfileObservation,
  ProfileLoginState,
  RefreshMaterialState,
} from '../types/api'

export interface ProfileFact {
  label: string
  value: string
}

const LOGIN_STATE_LABELS: Record<ProfileLoginState, string> = {
  logged_in: 'Logged in',
  logged_out: 'Logged out',
  expired: 'Expired',
  unknown: 'Unknown',
}

const REFRESH_MATERIAL_LABELS: Record<RefreshMaterialState, string> = {
  present: 'Present',
  missing: 'Missing',
  unknown: 'Unknown',
}

const EXPIRY_SOURCE_LABELS: Record<CredentialExpirySource, string> = {
  claude_oauth_expires_at: 'stored access-token expiry',
  codex_access_token_jwt_exp: 'access-token exp claim',
  codex_refresh_token_jwt_exp: 'refresh-token exp claim',
}

function utc(instant: string): string {
  return new Date(instant).toISOString().replace('.000Z', 'Z')
}

/**
 * Credential-safe profile facts for the Settings card. Access/session expiry and
 * refresh-credential expiry are separate rows: a CLI that stores no refresh
 * expiry shows "Not exposed by CLI", never the access-token expiry.
 */
export function executorProfileFacts(profile: ExecutorProfileObservation, now: Date): ProfileFact[] {
  const session = profile.session_expires_at && profile.session_expiry_source
    ? `${utc(profile.session_expires_at)} (${EXPIRY_SOURCE_LABELS[profile.session_expiry_source]}${
      new Date(profile.session_expires_at).getTime() <= now.getTime()
        ? profile.refresh_material === 'present' ? '; expired, renewable' : '; expired'
        : ''
    })`
    : 'Not stored'
  const refresh = profile.refresh_expires_at && profile.refresh_expiry_source
    ? `${utc(profile.refresh_expires_at)} (${EXPIRY_SOURCE_LABELS[profile.refresh_expiry_source]})`
    : 'Not exposed by CLI'
  return [
    { label: 'Login', value: LOGIN_STATE_LABELS[profile.login_state] },
    { label: 'Refresh credential', value: REFRESH_MATERIAL_LABELS[profile.refresh_material] },
    { label: 'Access/session expiry', value: session },
    { label: 'Refresh credential expiry', value: refresh },
    { label: 'Last refresh', value: profile.last_refresh_at ? utc(profile.last_refresh_at) : 'Not stored' },
  ]
}

/** The one-line attention notice a degraded or unavailable host-session profile needs. */
export function executorProfileAttention(diagnostic: ExecutorDiagnostic): string | null {
  const profile = diagnostic.profile
  if (!profile || profile.condition === 'healthy') return null
  if (diagnostic.availability === 'degraded') {
    return `Degraded: ${diagnostic.reason} New starts remain admitted until it expires; log in again now.`
  }
  if (diagnostic.availability === 'unavailable') {
    return `Unavailable: ${diagnostic.reason} New starts are refused until the profile is logged in again.`
  }
  return `Unknown: ${diagnostic.reason}`
}
