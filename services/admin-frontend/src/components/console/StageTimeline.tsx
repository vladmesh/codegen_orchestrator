import { cn } from '@/lib/utils'
import type { JourneyTimeline, TimelineStageState } from '@/types/api'
import { timelineRows, timelineTotal } from '@/pages/timelineView'

const stateStyles: Record<TimelineStageState, string> = {
  done: 'bg-teal-400',
  running: 'bg-teal-300 animate-pulse',
  unrecorded: 'border border-dashed border-zinc-600',
}

/** Every measured stage of the story with its duration, the waits between them and the total. */
export function StageTimeline({ timeline, now }: { timeline: JourneyTimeline; now: number }) {
  const rows = timelineRows(timeline, now)
  const total = timelineTotal(timeline)
  return (
    <section className="min-w-0 rounded-lg border border-border p-4" aria-labelledby="stage-timeline">
      <h2 id="stage-timeline" className="mb-3 text-sm font-semibold text-foreground">
        Stage timeline
      </h2>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No stage recorded yet</p>
      ) : (
        <ol className="space-y-1 text-sm">
          {rows.map((row) =>
            row.kind === 'stage' ? (
              <li key={row.key} className="flex items-center gap-2">
                <span className={cn('h-2 w-2 shrink-0 rounded-full', stateStyles[row.state])} />
                <span className={cn(row.state === 'unrecorded' ? 'text-muted-foreground' : 'text-foreground')}>{row.label}</span>
                <span
                  className={cn(
                    'ml-auto font-mono text-xs tabular-nums',
                    row.state === 'running' ? 'text-teal-300' : 'text-muted-foreground',
                  )}
                >
                  {row.duration}
                </span>
              </li>
            ) : (
              <li key={row.key} className="flex items-center gap-2 pl-4 text-xs text-amber-400/80">
                <span className="font-mono uppercase tracking-wider">wait</span>
                <span className="text-muted-foreground">{row.label}</span>
                <span className="ml-auto font-mono tabular-nums">{row.duration}</span>
              </li>
            ),
          )}
        </ol>
      )}
      <div className="mt-3 flex flex-wrap items-baseline gap-2 border-t border-border pt-3 text-sm">
        <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
          Brief confirmed to QA passed
        </span>
        {total.kind === 'total' ? (
          <span className="ml-auto font-mono font-semibold tabular-nums text-foreground">{total.duration}</span>
        ) : (
          <span className="ml-auto text-muted-foreground">{total.reason}</span>
        )}
      </div>
    </section>
  )
}
