import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import { RotateCcw } from 'lucide-react'
import { api } from '@/lib/api'
import { cn, formatDate } from '@/lib/utils'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { Passport } from '@/components/console/Passport'
import type { JourneyDetail, JourneyStage, JourneyStep, StepStatus } from '@/types/api'
import {
  STAGE_ACTORS,
  STAGE_LABELS,
  focusStage,
  formatDuration,
  journeyLanes,
  journeyWindow,
  progressIndex,
  retries,
  type BarTone,
} from './journeyView'

const knobStyles: Record<StepStatus, string> = {
  done: 'border-teal-400 bg-teal-400 text-zinc-950',
  active: 'border-teal-400 bg-zinc-950 text-teal-300 animate-pulse',
  waiting: 'border-amber-500 bg-amber-950 text-amber-300',
  failed: 'border-red-500 bg-red-950 text-red-300',
  pending: 'border-zinc-600 bg-zinc-950 text-zinc-500',
  skipped: 'border-dashed border-zinc-700 bg-zinc-950 text-zinc-600',
}

const barStyles: Record<BarTone, string> = {
  done: 'bg-teal-500/80',
  failed: 'bg-red-500/80',
  active: 'bg-teal-300 animate-pulse',
  waiting: 'bg-amber-500/80',
}

function Station({
  step,
  index,
  selected,
  onSelect,
  now,
}: {
  step: JourneyStep
  index: number
  selected: boolean
  onSelect: () => void
  now: number
}) {
  const retried = retries(step)
  return (
    <button
      type="button"
      role="tab"
      aria-selected={selected}
      onClick={onSelect}
      className="group relative flex flex-col items-center gap-1.5 px-1 text-center focus-visible:outline-none"
    >
      <span
        className={cn(
          'z-10 grid h-9 w-9 place-items-center rounded-full border-2 font-mono text-xs',
          knobStyles[step.status],
          selected && 'ring-4 ring-teal-400/30',
          'group-focus-visible:ring-4 group-focus-visible:ring-teal-400/60',
        )}
      >
        {retried > 0 && step.status === 'done' ? <RotateCcw className="h-3.5 w-3.5" /> : index + 1}
      </span>
      <span className="text-sm font-semibold leading-tight text-foreground">{STAGE_LABELS[step.stage]}</span>
      <span className="font-mono text-[10.5px] leading-tight text-muted-foreground">
        {step.status === 'skipped' ? 'skipped' : step.status === 'pending' ? '—' : formatDuration(step.started_at, step.finished_at, now)}
        {retried > 0 && <span className="block text-amber-400">{retried} failed</span>}
      </span>
    </button>
  )
}

function StepDetail({ step, now }: { step: JourneyStep; now: number }) {
  return (
    <div className="space-y-3">
      <div>
        <div className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
          {STAGE_ACTORS[step.stage]}
        </div>
        <h2 className="text-lg font-semibold text-foreground">{STAGE_LABELS[step.stage]}</h2>
        <StatusBadge status={step.status} />
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="font-mono text-xs text-muted-foreground">started</dt>
        <dd>{step.started_at ? formatDate(step.started_at) : '—'}</dd>
        <dt className="font-mono text-xs text-muted-foreground">finished</dt>
        <dd>{step.finished_at ? formatDate(step.finished_at) : '—'}</dd>
        {step.facts.map((fact) => (
          <div key={`${fact.label}-${fact.value}`} className="contents">
            <dt className="font-mono text-xs text-muted-foreground">{fact.label}</dt>
            <dd className="break-words">{fact.value}</dd>
          </div>
        ))}
      </dl>
      {step.attempts.length > 0 && (
        <div className="space-y-1.5">
          <div className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">attempts</div>
          {step.attempts.map((attempt) => (
            <div key={attempt.id} className="rounded border border-border p-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={attempt.status} />
                <span className="font-mono text-muted-foreground">{attempt.id}</span>
                {attempt.actor && <span className="text-muted-foreground">· {attempt.actor}</span>}
                <span className="ml-auto tabular-nums text-muted-foreground">
                  {formatDuration(attempt.started_at, attempt.finished_at, now)}
                </span>
              </div>
              {attempt.error && <p className="mt-1 break-words text-red-300">{attempt.error}</p>}
              {attempt.task_id && (
                <Link to={`/tasks/${attempt.task_id}`} className="mt-1 inline-block text-teal-300 hover:underline">
                  task {attempt.task_id}
                </Link>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function JourneyPage() {
  const { storyId } = useParams<{ storyId: string }>()
  const [selected, setSelected] = useState<JourneyStage | null>(null)
  const journey = useQuery({
    queryKey: ['admin-console-journey', storyId],
    queryFn: () => api.get<JourneyDetail>(`/admin/v2/journeys/${storyId}`),
    refetchInterval: 10_000,
    enabled: Boolean(storyId),
  })

  if (journey.isLoading) return <p className="text-muted-foreground">Loading…</p>
  if (journey.isError || !journey.data) {
    return <p role="alert" className="text-red-400">Journey request failed.</p>
  }

  const { summary, steps, passport } = journey.data
  // The moment of the last fetch, so a render never reads the clock itself.
  const now = journey.dataUpdatedAt
  const span = journeyWindow(steps, now)
  const lanes = span ? journeyLanes(steps, span, now) : []
  const current = selected ?? focusStage(steps)
  const currentStep = steps.find((s) => s.stage === current) ?? steps[0]
  const reached = progressIndex(steps)

  return (
    <div className="space-y-5">
      <div className="space-y-1">
        <div className="font-mono text-xs text-muted-foreground">
          <Link to="/journeys" className="hover:underline">Journeys</Link> /{' '}
          <Link to={`/projects/${summary.project_id}`} className="hover:underline">{summary.project_title}</Link> /{' '}
          <Link to={`/stories/${summary.story_id}`} className="hover:underline">{summary.story_id}</Link>
        </div>
        <h1 className="text-2xl font-bold text-foreground">{journey.data.request ?? summary.title}</h1>
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <StatusBadge status={summary.status} />
          {summary.waiting_on !== 'none' && (
            <span className="text-amber-400">waits on {summary.waiting_on.replace('_', ' ')}</span>
          )}
          <span>
            {summary.finished_at ? 'took ' : 'running for '}
            {formatDuration(summary.created_at, summary.finished_at, now)}
          </span>
          {journey.data.requirements !== null && <span>· {journey.data.requirements} requirements</span>}
          {journey.data.pr_number !== null && <span>· PR #{journey.data.pr_number}</span>}
        </div>
      </div>

      <div className="grid overflow-hidden rounded-lg border border-border lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="overflow-x-auto border-b border-border p-4 lg:border-b-0 lg:border-r">
          <div className="min-w-[760px]">
            <div className="relative grid grid-cols-8" role="tablist" aria-label="Journey stages">
              <div className="absolute left-[6.25%] right-[6.25%] top-[17px] h-[3px] rounded bg-zinc-800" />
              <div
                className="absolute left-[6.25%] top-[17px] h-[3px] rounded bg-teal-400"
                style={{ width: `${(reached / 7) * 87.5}%` }}
              />
              {steps.map((step, index) => (
                <Station
                  key={step.stage}
                  step={step}
                  index={index}
                  selected={step.stage === current}
                  onSelect={() => setSelected(step.stage)}
                  now={now}
                />
              ))}
            </div>
            {span && lanes.length > 0 && (
              <div className="mt-6 space-y-1.5">
                {lanes.map((lane) => (
                  <div key={lane.stage} className="grid grid-cols-[120px_1fr] items-center gap-2">
                    <span className="font-mono text-[10.5px] uppercase tracking-wider text-muted-foreground">{lane.actor}</span>
                    <div className="relative h-4 rounded bg-zinc-900">
                      {lane.bars.map((bar) => (
                        <span
                          key={bar.key}
                          title={bar.label}
                          className={cn('absolute top-0.5 h-3 rounded-sm', barStyles[bar.tone])}
                          style={{ left: `${bar.left}%`, width: `${bar.width}%` }}
                        />
                      ))}
                    </div>
                  </div>
                ))}
                <div className="grid grid-cols-[120px_1fr] gap-2 font-mono text-[10px] text-muted-foreground">
                  <span />
                  <div className="flex justify-between">
                    <span>{new Date(span.start).toLocaleTimeString()}</span>
                    <span>{new Date(span.end).toLocaleTimeString()}</span>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
        <aside className="bg-zinc-950/60 p-4" aria-live="polite">
          <StepDetail step={currentStep} now={now} />
        </aside>
      </div>

      <Passport passport={passport} />
    </div>
  )
}
