import type { LiveLogKind, LiveLogLine, LiveLogSource, LiveLogUnavailable, StoryStatus } from '../types/api'
import { parseApiTime } from '../lib/utils.ts'

/** How often an open live log asks for new lines while its run can still write them. */
export const LIVE_LOG_POLL_MS = 3_000

const TERMINAL_STORY: readonly StoryStatus[] = ['completed', 'failed', 'archived']

export function isTerminalStory(status: StoryStatus): boolean {
  return TERMINAL_STORY.includes(status)
}

/** The live log's refetch interval: polls while its run can write, stops once it cannot. */
export function liveLogInterval(live: boolean): number | false {
  return live ? LIVE_LOG_POLL_MS : false
}

const SOURCE_TAGS: Record<LiveLogSource, string> = {
  architect: 'architect',
  developer: 'dev',
  qa: 'qa',
  worker: 'worker',
}

export type KindTone = 'heading' | 'plain' | 'command' | 'path' | 'success' | 'error'

const KIND_TONES: Record<LiveLogKind, KindTone> = {
  step: 'heading',
  info: 'plain',
  cmd: 'command',
  file: 'path',
  ok: 'success',
  err: 'error',
}

export interface LiveLogRow {
  key: string
  time: string
  source: LiveLogSource
  tag: string
  tone: KindTone
  /** Shown before the text and kept out of a copied selection. */
  prompt: string | null
  text: string
}

/** Log lines as terminal rows, in the order the API sent them; `timeZone` defaults to the viewer's. */
export function liveLogRows(lines: LiveLogLine[], timeZone?: string): LiveLogRow[] {
  const clock = new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
    timeZone,
  })
  return lines.map((line) => ({
    key: `${line.attempt_id}:${line.seq}`,
    time: clock.format(parseApiTime(line.at)),
    source: line.source,
    tag: SOURCE_TAGS[line.source],
    tone: KIND_TONES[line.kind],
    prompt: line.kind === 'cmd' ? '$ ' : null,
    text: line.text,
  }))
}

const UNAVAILABLE_MESSAGES: Record<LiveLogUnavailable, string> = {
  log_store_not_configured: 'The log store is not configured on this installation.',
  log_store_query_failed: 'The log store query failed.',
  log_store_timed_out: 'The log store did not answer in time.',
}

export function unavailableMessage(reason: LiveLogUnavailable): string {
  return UNAVAILABLE_MESSAGES[reason]
}

/** The notice for attempts the API left unread, or null when it read them all. */
export function omittedMessage(omitted: number): string | null {
  if (omitted === 0) return null
  return omitted === 1
    ? 'The oldest attempt is not shown: it is beyond the query limit.'
    : `The ${omitted} oldest attempts are not shown: they are beyond the query limit.`
}
