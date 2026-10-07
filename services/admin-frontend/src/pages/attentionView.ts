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
