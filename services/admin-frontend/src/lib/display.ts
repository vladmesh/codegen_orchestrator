/** Human-facing formatting for labels and timestamps. Pure: no env, no clock reads. */

const ACRONYMS = new Set(['qa', 'ci', 'pr', 'api', 'id', 'url', 'ui', 'e2e'])

/** `product_owner` → `Product Owner`, `in_dev` → `In Dev`, `qa` → `QA`. */
export function humanizeLabel(value: string | null | undefined): string {
  if (!value) return '—'
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((word) => (ACRONYMS.has(word.toLowerCase()) ? word.toUpperCase() : word[0].toUpperCase() + word.slice(1)))
    .join(' ')
}

const sameYear = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})
const otherYear = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})
const clock = new Intl.DateTimeFormat('en-US', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const clockSec = new Intl.DateTimeFormat('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })

/** `23:03:41`, for log lines. */
export function logTime(value: string | number): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '--:--:--' : clockSec.format(date)
}

/** `Oct 12, 23:03`; the year is added only when it differs from `now`'s. */
export function shortDateTime(iso: string, now: number): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return (date.getFullYear() === new Date(now).getFullYear() ? sameYear : otherYear).format(date)
}

/** `23:03`. */
export function clockTime(value: string | number): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : clock.format(date)
}
