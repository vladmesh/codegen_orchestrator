import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

const TIME_ZONE_DESIGNATOR = /(?:Z|[+-]\d{2}(?::?\d{2})?)$/i

/** An API date-time string as an instant: one with no `Z` or offset is UTC, as the API stores it. */
export function parseApiTime(iso: string): Date {
  // A date-only string is already UTC in JavaScript, and its `-DD` tail looks like an offset.
  return new Date(iso.includes('T') && !TIME_ZONE_DESIGNATOR.test(iso) ? `${iso}Z` : iso)
}

export function formatDate(iso: string): string {
  return parseApiTime(iso).toLocaleString()
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return 'Unavailable'
  const diff = Date.now() - parseApiTime(iso).getTime()
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  const value = bytes / Math.pow(1024, i)
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

export function freshnessColor(iso: string, thresholdMinutes = 5): 'text-green-400' | 'text-yellow-400' | 'text-red-400' {
  const ageMinutes = (Date.now() - parseApiTime(iso).getTime()) / 60_000
  if (ageMinutes <= thresholdMinutes) return 'text-green-400'
  if (ageMinutes <= thresholdMinutes * 2) return 'text-yellow-400'
  return 'text-red-400'
}

/** The browsable URL of a repository, or null while its `git_url` is not an http(s) URL yet. */
export function repositoryWebUrl(gitUrl: string | null | undefined): string | null {
  if (!gitUrl || !/^https?:\/\//i.test(gitUrl)) return null
  return gitUrl.replace(/\.git$/, '')
}
