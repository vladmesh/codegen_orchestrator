import { useLayoutEffect, useRef, useState } from 'react'
import { TerminalSquare } from 'lucide-react'
import { cn } from '@/lib/utils'
import { logTime } from '@/lib/display'
import { reducedMotion } from '@/lib/live'
import { useJourneyLive } from '@/lib/useJourneyLive'
import { LiveDot } from '@/components/ui/LiveDot'
import { sourceTag, tagTone, type TagTone } from '@/pages/liveFeed'
import type { LiveLineKind } from '@/types/api'

const kindStyles: Record<LiveLineKind, string> = {
  cmd: 'text-sky-300',
  out: 'text-zinc-400',
  ok: 'text-emerald-400',
  err: 'text-red-400',
  info: 'text-zinc-200',
  file: 'text-violet-300',
  step: 'font-semibold text-zinc-50',
}

const tagStyles: Record<TagTone, string> = {
  architect: 'text-teal-300',
  dev: 'text-amber-300',
  ci: 'text-violet-300',
  ship: 'text-cyan-300',
  brimos: 'text-zinc-100',
}

/**
 * Demo build: a terminal-like feed of what the agents do (commands, files, test runs, CI).
 * New lines fade in and the panel follows the tail unless the viewer scrolled up.
 */
export function LiveLog({
  storyId,
  taskId,
  live,
  className,
  title = 'Live log',
}: {
  storyId: string | undefined
  taskId?: string
  /** Story still running: shows the live dot. */
  live: boolean
  className?: string
  title?: string
}) {
  const { data } = useJourneyLive(storyId, taskId)
  const lines = data?.lines ?? []
  const last = lines.length ? lines[lines.length - 1].n : 0

  // Lines present when the panel opened are shown as is; only later ones fade in.
  const [baseline, setBaseline] = useState<number | null>(null)
  if (baseline === null && data) setBaseline(last)

  const box = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  useLayoutEffect(() => {
    const el = box.current
    if (!el || !follow.current) return
    el.scrollTo({ top: el.scrollHeight, behavior: baseline === null || reducedMotion() ? 'auto' : 'smooth' })
  }, [last, baseline])

  return (
    <section
      aria-labelledby="live-log"
      className={cn('flex min-h-0 flex-col overflow-hidden rounded-lg border border-zinc-800 bg-[#0b0d10]', className)}
    >
      <div className="flex items-center gap-2 border-b border-zinc-800 px-3 py-2">
        <TerminalSquare className="h-4 w-4 text-zinc-400" />
        <h2 id="live-log" className="text-sm font-semibold text-foreground">
          {title}
        </h2>
        {live && lines.length > 0 && <LiveDot tone="teal" label="live" className="ml-1" />}
        <span className="ml-auto font-mono text-[11px] text-zinc-500">{data?.repo ?? ''}</span>
      </div>
      <div
        ref={box}
        onScroll={(e) => {
          const el = e.currentTarget
          follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
        }}
        className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-3 py-2 font-mono text-[12px] leading-[1.55]"
        role="log"
        aria-live="off"
      >
        {lines.length === 0 ? (
          <p className="text-zinc-500">{data ? 'Waiting for the first agent…' : 'Connecting…'}</p>
        ) : (
          lines.map((line) => (
            <div
              key={line.n}
              className={cn(
                'grid grid-cols-[58px_70px_minmax(0,1fr)] gap-x-2 rounded-sm',
                baseline !== null && line.n > baseline && 'log-enter',
                line.kind === 'err' && 'bg-red-500/10',
                line.kind === 'step' && 'mt-1.5',
              )}
            >
              <span className="tabular-nums text-zinc-600">{logTime(line.at)}</span>
              <span className={cn('truncate', tagStyles[tagTone(line.source)])}>{sourceTag(line.source)}</span>
              <span className={cn('whitespace-pre-wrap break-words', kindStyles[line.kind])}>
                {line.kind === 'cmd' && <span className="select-none text-zinc-500">$ </span>}
                {line.text}
              </span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}
