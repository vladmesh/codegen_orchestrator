import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import { ExternalLink, GitPullRequest, RotateCcw } from 'lucide-react'
import { api } from '@/lib/api'
import { cn, formatClock, formatDate, isDemo } from '@/lib/utils'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { Passport } from '@/components/console/Passport'
import { TaskBoard } from '@/components/console/TaskBoard'
import { LiveLog } from '@/components/console/LiveLog'
import { RepoPanel } from '@/components/console/RepoPanel'
import { humanizeLabel } from '@/lib/display'
import type { JourneyAttempt, JourneyDetail, JourneyStage, JourneyStep, StepStatus, Task } from '@/types/api'
import {
  STAGE_ACTORS,
  STAGE_LABELS,
  focusStage,
  formatDuration,
  journeyLanes,
  journeyWindow,
  linkedPullRequests,
  progressIndex,
  retries,
  type BarTone,
  type Lane,
  type LinkedPullRequest,
} from './journeyView'
import { enterClass, livePoll, useFlash } from '@/lib/live'

const knobStyles: Record<StepStatus, string> = {
  done: 'border-teal-400 bg-teal-400 text-zinc-950',
  // demo build: a soft ring breathes around it instead (see Station)
  active: isDemo ? 'border-teal-400 bg-zinc-950 text-teal-300' : 'border-teal-400 bg-zinc-950 text-teal-300 animate-pulse',
  waiting: 'border-amber-500 bg-amber-950 text-amber-300',
  failed: 'border-red-500 bg-red-950 text-red-300',
  pending: 'border-zinc-600 bg-zinc-950 text-zinc-500',
  skipped: 'border-dashed border-zinc-700 bg-zinc-950 text-zinc-600',
}

// Demo build: role names a viewer reads at a glance, instead of internal worker kinds.
const DEMO_ACTORS: Partial<Record<JourneyStage, string>> = {
  build: 'Developer',
  deploy: 'Deployer',
  verify: 'QA',
}

/**
 * Demo build: every working stage keeps its lane from the start (an empty track until it
 * begins), so lanes appearing mid-scene never push the page down. Bar positions are rounded,
 * so a poll whose data did not move does not touch the DOM.
 */
function demoLanes(lanes: Lane[], steps: JourneyStep[]): Lane[] {
  if (!isDemo) return lanes
  const round = (v: number) => Math.round(v * 20) / 20
  return steps
    .filter((s) => s.stage !== 'live')
    .map((s) => {
      const lane = lanes.find((l) => l.stage === s.stage)
      const bars = (lane?.bars ?? []).map((b) => ({ ...b, left: round(b.left), width: round(b.width) }))
      return { stage: s.stage, actor: lane?.actor ?? STAGE_ACTORS[s.stage], bars }
    })
}

function stageActor(stage: JourneyStage): string {
  return (isDemo && DEMO_ACTORS[stage]) || STAGE_ACTORS[stage]
}

/** Demo build: what an attempt worked on, in words — the task title, or the run kind. */
function attemptLabel(attempt: JourneyAttempt, step: JourneyStep, taskTitles: Map<string, string>): string {
  if (attempt.task_id && taskTitles.has(attempt.task_id)) return taskTitles.get(attempt.task_id)!
  if (attempt.kind === 'deploy') return 'Deploy to server'
  if (attempt.kind === 'qa') return 'Live checks'
  const ciRuns = step.attempts.filter((a) => !a.task_id)
  return `CI run ${ciRuns.indexOf(attempt) + 1}`
}

const barStyles: Record<BarTone, string> = {
  done: 'bg-teal-500/80',
  failed: 'bg-red-500/80',
  active: isDemo ? 'bg-teal-300 live-shimmer' : 'bg-teal-300 animate-pulse',
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
  const knob = useFlash<HTMLSpanElement>(step.status, step.status === 'failed' ? 'red' : 'teal')
  return (
    <button
      type="button"
      role="tab"
      aria-selected={selected}
      onClick={onSelect}
      className="group relative flex flex-col items-center gap-1.5 px-1 text-center focus-visible:outline-none"
    >
      <span
        ref={knob}
        className={cn(
          'relative z-10 grid h-9 w-9 place-items-center rounded-full border-2 font-mono text-xs',
          isDemo && 'demo-smooth transition-colors duration-500',
          knobStyles[step.status],
          selected && 'ring-4 ring-teal-400/30',
          'group-focus-visible:ring-4 group-focus-visible:ring-teal-400/60',
        )}
      >
        {isDemo && step.status === 'active' && (
          <span aria-hidden className="live-ring absolute -inset-[3px] rounded-full border-2 border-teal-400" />
        )}
        {retried > 0 && step.status === 'done' ? <RotateCcw className="h-3.5 w-3.5" /> : index + 1}
      </span>
      <span className="text-sm font-semibold leading-tight text-foreground">{STAGE_LABELS[step.stage]}</span>
      <span className={cn('font-mono text-[10.5px] leading-tight text-muted-foreground', isDemo && 'min-h-[26px]')}>
        {step.status === 'skipped' ? 'skipped' : step.status === 'pending' ? '—' : formatDuration(step.started_at, step.finished_at, now)}
        {retried > 0 && <span className="block text-amber-400">{retried} failed</span>}
      </span>
    </button>
  )
}

/** One attempt box; in the demo build it slides in when new and glows when its status changes. */
function AttemptRow({ status, children }: { status: string; children: React.ReactNode }) {
  const failed = !['queued', 'running', 'completed', 'published'].includes(status)
  const ref = useFlash<HTMLDivElement>(status, failed ? 'red' : 'teal')
  return (
    <div ref={ref} className={cn('rounded border border-border p-2 text-xs', enterClass)}>
      {children}
    </div>
  )
}

function StepDetail({ step, now, taskTitles }: { step: JourneyStep; now: number; taskTitles: Map<string, string> }) {
  return (
    <div className="space-y-3">
      <div>
        <div className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
          {stageActor(step.stage)}
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
            <AttemptRow key={attempt.id} status={attempt.status}>
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={attempt.status} />
                {isDemo ? (
                  attempt.task_id ? (
                    <Link to={`/tasks/${attempt.task_id}`} className="min-w-0 text-foreground hover:underline">
                      {attemptLabel(attempt, step, taskTitles)}
                    </Link>
                  ) : (
                    <span className="min-w-0 text-foreground">{attemptLabel(attempt, step, taskTitles)}</span>
                  )
                ) : (
                  <span className="font-mono text-muted-foreground">{attempt.id}</span>
                )}
                {attempt.actor && <span className="text-muted-foreground">· {attempt.actor}</span>}
                <span className="ml-auto tabular-nums text-muted-foreground">
                  {formatDuration(attempt.started_at, attempt.finished_at, now)}
                </span>
              </div>
              {attempt.error && <p className="mt-1 break-words text-red-300">{attempt.error}</p>}
              {!isDemo && attempt.task_id && (
                <Link to={`/tasks/${attempt.task_id}`} className="mt-1 inline-block text-teal-300 hover:underline">
                  task {attempt.task_id}
                </Link>
              )}
            </AttemptRow>
          ))}
        </div>
      )}
    </div>
  )
}

function LinkedPullRequests({ links }: { links: LinkedPullRequest[] }) {
  if (links.length === 0) return null
  return (
    <section className="rounded-lg border border-border p-4" aria-labelledby="linked-pull-requests">
      <h2 id="linked-pull-requests" className="mb-3 text-sm font-semibold text-foreground">
        Linked pull requests
      </h2>
      <ul className="space-y-2">
        {links.map((link) => (
          <li key={link.key}>
            <a
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              className="group flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-border p-3 hover:border-teal-400/60"
            >
              <GitPullRequest className="h-4 w-4 shrink-0 text-teal-300" />
              <span className="font-mono text-[11px] uppercase tracking-wider text-teal-300">{link.label}</span>
              <span className="min-w-0 flex-1 break-words text-sm text-foreground group-hover:underline">{link.title}</span>
              {link.repo && <span className="font-mono text-xs text-muted-foreground">{link.repo}</span>}
              {link.number && <span className="font-mono text-xs text-foreground">{link.number}</span>}
              <span className="text-xs text-muted-foreground">{formatDate(link.createdAt)}</span>
              <ExternalLink className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            </a>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function JourneyPage() {
  const { storyId } = useParams<{ storyId: string }>()
  const [selected, setSelected] = useState<JourneyStage | null>(null)
  const journey = useQuery({
    queryKey: ['admin-console-journey', storyId],
    queryFn: () => api.get<JourneyDetail>(`/admin/v2/journeys/${storyId}`),
    refetchInterval: livePoll(10_000),
    enabled: Boolean(storyId),
  })
  // Demo build names attempts by their task, so it needs the story's task titles.
  const tasks = useQuery({
    queryKey: ['tasks', 'story', storyId],
    queryFn: () => api.get<Task[]>(`/tasks/?story_id=${storyId}&limit=200`),
    refetchInterval: livePoll(10_000),
    enabled: isDemo && Boolean(storyId),
  })

  if (journey.isLoading) return <p className="text-muted-foreground">Loading…</p>
  if (journey.isError || !journey.data) {
    return <p role="alert" className="text-red-400">Journey request failed.</p>
  }

  const { summary, steps, passport } = journey.data
  // The moment of the last fetch, so a render never reads the clock itself.
  const now = journey.dataUpdatedAt
  const span = journeyWindow(steps, now)
  const lanes = span ? demoLanes(journeyLanes(steps, span, now), steps) : []
  const current = selected ?? focusStage(steps)
  const currentStep = steps.find((s) => s.stage === current) ?? steps[0]
  const reached = progressIndex(steps)
  const taskTitles = new Map((tasks.data ?? []).map((t) => [t.id, t.title]))

  return (
    <div className="space-y-5">
      <div className="space-y-1">
        <div className="font-mono text-xs text-muted-foreground">
          <Link to="/journeys" className="hover:underline">Journeys</Link> /{' '}
          <Link to={`/projects/${summary.project_id}`} className="hover:underline">{summary.project_title}</Link> /{' '}
          <Link to={`/stories/${summary.story_id}`} className="hover:underline">
            {isDemo ? summary.title : summary.story_id}
          </Link>
        </div>
        <h1 className="text-2xl font-bold text-foreground">
          {isDemo ? summary.title : (journey.data.request ?? summary.title)}
        </h1>
        {isDemo && journey.data.request && (
          <p className="max-w-3xl text-sm italic text-muted-foreground">“{journey.data.request}”</p>
        )}
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <StatusBadge status={summary.status} />
          {summary.waiting_on !== 'none' && (
            <span className="text-amber-400">waits on {isDemo ? humanizeLabel(summary.waiting_on) : summary.waiting_on.replace('_', ' ')}</span>
          )}
          <span>
            {summary.finished_at ? 'took ' : 'running for '}
            {formatDuration(summary.created_at, summary.finished_at, now)}
          </span>
          {journey.data.requirements !== null && <span>· {journey.data.requirements} requirements</span>}
          {journey.data.pr_number !== null && <span>· PR #{journey.data.pr_number}</span>}
        </div>
      </div>

      <div
        className={cn(
          'grid overflow-hidden rounded-lg border border-border lg:grid-cols-[minmax(0,1fr)_320px]',
          // demo: one fixed height for the whole scene, the side panel scrolls instead of pushing the page
          isDemo && 'lg:h-[384px]',
        )}
      >
        <div className="overflow-x-auto border-b border-border p-4 lg:border-b-0 lg:border-r">
          <div className="min-w-[760px]">
            <div className="relative grid grid-cols-8" role="tablist" aria-label="Journey stages">
              <div className="absolute left-[6.25%] right-[6.25%] top-[17px] h-[3px] rounded bg-zinc-800" />
              <div
                className={cn(
                  'absolute left-[6.25%] top-[17px] h-[3px] rounded bg-teal-400',
                  isDemo && 'demo-smooth transition-[width] duration-1000 ease-in-out',
                )}
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
                  <div key={lane.stage} className={cn('grid grid-cols-[120px_1fr] items-center gap-2', enterClass)}>
                    <span className="font-mono text-[10.5px] uppercase tracking-wider text-muted-foreground">{isDemo ? stageActor(lane.stage) : lane.actor}</span>
                    <div className="relative h-4 rounded bg-zinc-900">
                      {lane.bars.map((bar) => (
                        <span
                          key={bar.key}
                          title={bar.label}
                          className={cn(
                            'absolute top-0.5 h-3 rounded-sm',
                            barStyles[bar.tone],
                            // bars grow with the clock: glide between polls instead of stepping
                            isDemo && 'demo-smooth transition-[left,width,background-color] duration-[1500ms] ease-linear',
                          )}
                          style={{ left: `${bar.left}%`, width: `${bar.width}%` }}
                        />
                      ))}
                    </div>
                  </div>
                ))}
                <div className="grid grid-cols-[120px_1fr] gap-2 font-mono text-[10px] text-muted-foreground">
                  <span />
                  <div className="flex justify-between">
                    <span>{formatClock(span.start)}</span>
                    <span>{formatClock(span.end)}</span>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
        <aside className={cn('bg-zinc-950/60 p-4', isDemo && 'overflow-y-auto')} aria-live="polite">
          <StepDetail step={currentStep} now={now} taskTitles={taskTitles} />
        </aside>
      </div>

      {isDemo && (
        // board and live log side by side, one height, so the frame never jumps
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_600px]">
          <TaskBoard tasks={tasks.data ?? []} steps={steps} prNumber={journey.data.pr_number} ready={tasks.isSuccess} />
          <LiveLog storyId={storyId} live={!summary.finished_at} className="h-[459px]" />
        </div>
      )}
      {isDemo && <RepoPanel storyId={storyId} />}

      <LinkedPullRequests links={linkedPullRequests(journey.data.links)} />

      <Passport passport={passport} />
    </div>
  )
}
