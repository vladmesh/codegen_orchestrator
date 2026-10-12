import type { AttentionItem } from '../types/api'

/** Where an attention row leads: the journey first, then the most specific detail page. */
export function attentionLink(item: AttentionItem): string | null {
  if (item.story_id) return `/journeys/${item.story_id}`
  if (item.task_id) return `/tasks/${item.task_id}`
  if (item.application_id !== null) return `/applications/${item.application_id}`
  if (item.server_handle) return '/servers'
  if (item.kind === 'queue') return '/queues'
  return null
}

export function severityCounts(items: AttentionItem[]): Record<AttentionItem['severity'], number> {
  const counts = { critical: 0, warning: 0, info: 0 }
  for (const item of items) counts[item.severity] += 1
  return counts
}

export function formatLeadTime(minutes: number | null): string {
  if (minutes === null) return '—'
  if (minutes < 60) return `${Math.round(minutes)} min`
  return `${(minutes / 60).toFixed(1)} h`
}

export interface AttentionScope {
  /** Query string for `/admin/v2/attention`, empty or starting with `?`. */
  query: string
  /** Human label of the active scope, or null when the feed is unscoped. */
  label: string | null
}

/**
 * Feed scope from the page URL: `?since=` takes an ISO timestamp or a relative `30m`/`6h`/`2d`,
 * `?project=` a project id.
 */
export function attentionScope(params: URLSearchParams, now: Date): AttentionScope {
  const out = new URLSearchParams()
  const labels: string[] = []
  const rawSince = params.get('since')
  if (rawSince) {
    const relative = /^(\d+)([mhd])$/.exec(rawSince)
    const unitMs = { m: 60_000, h: 3_600_000, d: 86_400_000 }
    const since = relative
      ? new Date(now.getTime() - Number(relative[1]) * unitMs[relative[2] as 'm' | 'h' | 'd'])
      : new Date(rawSince)
    if (!Number.isNaN(since.getTime())) {
      out.set('since', since.toISOString())
      labels.push(relative ? `last ${rawSince}` : `since ${since.toLocaleString()}`)
    }
  }
  const project = params.get('project')
  if (project) {
    out.set('project_id', project)
    labels.push('one project')
  }
  const query = out.toString()
  return { query: query ? `?${query}` : '', label: labels.length ? labels.join(', ') : null }
}
