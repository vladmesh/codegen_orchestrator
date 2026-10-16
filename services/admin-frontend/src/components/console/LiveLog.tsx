import { useEffect, useLayoutEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Terminal } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { LiveLogResponse, LiveLogSource } from '@/types/api'
import { liveLogInterval, liveLogRows, omittedMessage, unavailableMessage, type KindTone } from '@/pages/liveLogView'

const toneStyles: Record<KindTone, string> = {
  heading: 'font-semibold text-zinc-50',
  plain: 'text-zinc-300',
  command: 'text-sky-300',
  path: 'text-violet-300',
  success: 'text-emerald-400',
  error: 'text-red-400',
}

const tagStyles: Record<LiveLogSource, string> = {
  architect: 'text-teal-300',
  developer: 'text-amber-300',
  qa: 'text-cyan-300',
  worker: 'text-zinc-400',
}

// Within this many pixels of the bottom the reader counts as following the tail.
const FOLLOW_SLACK_PX = 48

/** A terminal view of an `/admin/v2/.../live-log` read that follows the tail unless the reader scrolled up. */
export function LiveLog({ path, live, className }: { path: string; live: boolean; className?: string }) {
  const log = useQuery({
    queryKey: ['admin-console-live-log', path],
    queryFn: () => api.get<LiveLogResponse>(path),
    refetchInterval: liveLogInterval(live),
  })
  const { refetch } = log
  // One more read once the run ends picks up the lines it wrote after the previous poll.
  const wasLive = useRef(live)
  useEffect(() => {
    if (wasLive.current && !live) void refetch()
    wasLive.current = live
  }, [live, refetch])

  const rows = log.data ? liveLogRows(log.data.lines) : []
  const lastKey = rows.length ? rows[rows.length - 1].key : null
  const omitted = log.data ? omittedMessage(log.data.omitted_attempts) : null

  const box = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  useLayoutEffect(() => {
    const el = box.current
    if (el && follow.current) el.scrollTop = el.scrollHeight
  }, [lastKey])

  return (
    <section
      aria-labelledby="live-log"
      className={cn('flex min-h-0 flex-col overflow-hidden rounded-lg border border-zinc-800 bg-[#0b0d10]', className)}
    >
      <div className="flex items-center gap-2 border-b border-zinc-800 px-3 py-2">
        <Terminal className="h-4 w-4 text-zinc-400" />
        <h2 id="live-log" className="text-sm font-semibold text-foreground">
          Live log
        </h2>
        {live && <span className="ml-1 font-mono text-[11px] text-teal-300 animate-pulse">live</span>}
      </div>
      {log.data?.unavailable && (
        <p role="alert" className="border-b border-zinc-800 px-3 py-2 text-sm text-amber-300">
          {unavailableMessage(log.data.unavailable)}
        </p>
      )}
      {omitted && <p className="border-b border-zinc-800 px-3 py-2 text-xs text-zinc-400">{omitted}</p>}
      {log.isError && (
        <p role="alert" className="border-b border-zinc-800 px-3 py-2 text-sm text-red-400">
          Live log request failed.
        </p>
      )}
      <div
        ref={box}
        onScroll={(e) => {
          const el = e.currentTarget
          follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_SLACK_PX
        }}
        className="max-h-[480px] min-h-[120px] overflow-y-auto overflow-x-hidden px-3 py-2 font-mono text-[12px] leading-[1.55]"
        role="log"
        aria-live="off"
      >
        {rows.length === 0 ? (
          <p className="text-zinc-500">{log.isLoading ? 'Loading…' : 'No lines yet.'}</p>
        ) : (
          rows.map((row) => (
            <div
              key={row.key}
              className={cn(
                'grid grid-cols-[64px_72px_minmax(0,1fr)] gap-x-2 rounded-sm',
                row.tone === 'error' && 'bg-red-500/10',
                row.tone === 'heading' && 'mt-1.5',
              )}
            >
              <span className="tabular-nums text-zinc-600">{row.time}</span>
              <span className={cn('truncate', tagStyles[row.source])}>{row.tag}</span>
              <span className={cn('whitespace-pre-wrap break-words', toneStyles[row.tone])}>
                {row.prompt && <span className="select-none text-zinc-500">{row.prompt}</span>}
                {row.text}
              </span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}
