import { useLayoutEffect, useRef } from 'react'
import { Link } from 'react-router'
import { Check, GitPullRequest, Sparkles, Wrench } from 'lucide-react'
import { cn } from '@/lib/utils'
import { flashElement, reducedMotion } from '@/lib/live'
import { LiveDot } from '@/components/ui/LiveDot'
import type { JourneyStep, Task } from '@/types/api'
import {
  boardColumns,
  cardStatusLabel,
  cardTitle,
  isFixTask,
  prCard,
  type BoardColumnId,
  type PrCard,
  type PrTone,
} from '@/pages/taskBoard'

const badgeStyles: Record<string, string> = {
  backlog: 'bg-zinc-800 text-zinc-300',
  todo: 'bg-sky-950 text-sky-300',
  in_dev: 'bg-amber-950 text-amber-300',
  in_ci: 'bg-violet-950 text-violet-300',
  testing: 'bg-cyan-950 text-cyan-300',
  done: 'bg-teal-950 text-teal-300',
  failed: 'bg-red-950 text-red-300',
  blocked: 'bg-red-950 text-red-300',
}

const columnAccent: Record<BoardColumnId, string> = {
  backlog: 'bg-zinc-500',
  in_dev: 'bg-amber-400',
  review: 'bg-orange-400',
  in_ci: 'bg-violet-400',
  qa: 'bg-cyan-400',
  done: 'bg-teal-400',
}

const prTone: Record<PrTone, { card: string; badge: string }> = {
  running: { card: 'border-violet-500/50', badge: 'bg-violet-950 text-violet-300 live-breathe' },
  failed: { card: 'border-red-500/70 bg-red-950/30', badge: 'bg-red-900 text-red-100' },
  passed: { card: 'border-teal-500/40', badge: 'bg-teal-950 text-teal-300' },
}

/**
 * FLIP: cards keep a `data-card` key; after each render, a card that existed before slides
 * from its old box to the new one, and a new card fades in (new ones staggered, so the
 * architect's tasks land one by one). A card whose `data-sig` (status, assignee) changed also
 * glows for a moment — red when it turns into a failure. Web Animations API only — no dependency.
 */
function useCardFlip(container: React.RefObject<HTMLDivElement | null>, signature: string, ready: boolean) {
  const rects = useRef<Map<string, DOMRect>>(new Map())
  const sigs = useRef<Map<string, string>>(new Map())
  const mounted = useRef(false)
  useLayoutEffect(() => {
    const root = container.current
    if (!root) return
    const cards = Array.from(root.querySelectorAll<HTMLElement>('[data-card]'))
    const next = new Map<string, DOMRect>()
    const nextSigs = new Map<string, string>()
    let fresh = 0
    const animate = mounted.current && !reducedMotion()
    // Boxes are kept relative to the board, so page scrolling between polls is not a "move".
    const origin = root.getBoundingClientRect()
    for (const el of cards) {
      const key = el.dataset.card!
      const r = el.getBoundingClientRect()
      const box = new DOMRect(r.left - origin.left, r.top - origin.top, r.width, r.height)
      next.set(key, box)
      const sig = el.dataset.sig ?? ''
      nextSigs.set(key, sig)
      if (!animate) continue
      const prev = rects.current.get(key)
      const prevSig = sigs.current.get(key)
      if (prevSig !== undefined && prevSig !== sig) flashElement(el, el.dataset.alarm ? 'red' : 'teal')
      if (prev) {
        const dx = prev.left - box.left
        const dy = prev.top - box.top
        if (Math.abs(dx) > 1 || Math.abs(dy) > 1) {
          el.animate(
            [
              { transform: `translate(${dx}px, ${dy}px)`, boxShadow: '0 12px 32px rgba(45,212,191,0.25)' },
              { transform: 'translate(0, 0)', boxShadow: '0 0 0 rgba(0,0,0,0)' },
            ],
            { duration: 650, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' },
          )
        }
      } else {
        el.animate(
          [
            { opacity: 0, transform: 'translateY(-10px) scale(0.97)' },
            { opacity: 1, transform: 'none' },
          ],
          { duration: 450, delay: fresh * 220, easing: 'ease-out', fill: 'backwards' },
        )
        fresh += 1
      }
    }
    rects.current = next
    sigs.current = nextSigs
    // The first loaded state is shown as is; only later changes animate.
    if (ready) mounted.current = true
  }, [container, signature, ready])
}

function TaskCard({ task }: { task: Task }) {
  const fix = isFixTask(task)
  const status = task.status
  const working = status === 'in_dev'
  // Done cards fold to one line, so the Done column never outgrows the reserved board height.
  if (status === 'done') {
    return (
      <Link
        to={`/tasks/${task.id}`}
        data-card={task.id}
        data-sig={`${status}|${task.assignee ?? ''}`}
        className={cn(
          'relative flex items-center gap-1.5 overflow-hidden rounded-md border bg-zinc-900/60 py-1.5 pl-3 pr-2.5 transition-colors hover:border-teal-400/60',
          fix ? 'border-red-500/40' : 'border-zinc-800',
        )}
      >
        {fix && <span className="absolute inset-y-0 left-0 w-1 bg-red-500/70" />}
        <Check className="h-3.5 w-3.5 shrink-0 text-teal-400" />
        <span className="min-w-0 truncate text-[12.5px] text-zinc-300">{cardTitle(task)}</span>
        <span className="demo-smooth ml-auto shrink-0 rounded-full bg-teal-950 px-1.5 py-px text-[11px] font-medium text-teal-300 transition-colors duration-500">
          {cardStatusLabel(status)}
        </span>
      </Link>
    )
  }
  return (
    <Link
      to={`/tasks/${task.id}`}
      data-card={task.id}
      data-sig={`${status}|${task.assignee ?? ''}`}
      data-alarm={status === 'failed' || status === 'blocked' ? '1' : undefined}
      className={cn(
        'relative block overflow-hidden rounded-md border bg-zinc-900/80 py-2 pl-3 pr-2.5 transition-colors hover:border-teal-400/60',
        fix ? 'border-red-500/60 bg-red-950/25' : 'border-zinc-800',
      )}
    >
      {fix && <span className="absolute inset-y-0 left-0 w-1 bg-red-500" />}
      <div className="flex items-start gap-1.5">
        {fix && (
          <span className="mt-px inline-flex shrink-0 items-center gap-0.5 rounded bg-red-600 px-1 py-px font-mono text-[9.5px] font-bold uppercase tracking-wider text-white">
            <Wrench className="h-2.5 w-2.5" />
            fix
          </span>
        )}
        <span className="line-clamp-2 text-[13px] font-medium leading-snug text-foreground">{cardTitle(task)}</span>
      </div>
      <div className="mt-1.5 flex items-center gap-2 text-[11px]">
        {working && <LiveDot tone="amber" label="working" />}
        <span className="min-w-0 truncate text-muted-foreground">
          {task.assignee ?? (status === 'todo' ? 'Waiting for a developer' : 'Not started')}
        </span>
        <span
          className={cn(
            'demo-smooth ml-auto shrink-0 rounded-full px-1.5 py-px font-medium transition-colors duration-500',
            badgeStyles[status] ?? 'bg-zinc-800 text-zinc-300',
            (status === 'in_dev' || status === 'in_ci') && 'live-breathe',
          )}
        >
          {cardStatusLabel(status)}
        </span>
      </div>
      {working && <span aria-hidden className="live-shimmer absolute inset-x-0 bottom-0 h-0.5 bg-amber-400/40" />}
    </Link>
  )
}

function PullRequestCard({ card }: { card: PrCard }) {
  const tone = prTone[card.tone]
  return (
    <div
      data-card="pull-request"
      data-sig={`${card.tone}|${card.status}`}
      data-alarm={card.tone === 'failed' ? '1' : undefined}
      className={cn('demo-smooth relative overflow-hidden rounded-md border bg-zinc-900/80 py-2 pl-3 pr-2.5 transition-colors duration-500', tone.card)}
    >
      {card.tone === 'failed' && <span className="absolute inset-y-0 left-0 w-1 bg-red-500" />}
      <div className="flex items-center gap-1.5 text-[13px] font-medium text-foreground">
        <GitPullRequest className="h-3.5 w-3.5 shrink-0 text-violet-300" />
        {card.title}
      </div>
      <div className="mt-1.5 flex items-center gap-2 text-[11px]">
        <span className="min-w-0 truncate text-muted-foreground">{card.detail && card.tone !== 'failed' ? card.detail : 'GitHub CI'}</span>
        <span className={cn('demo-smooth ml-auto shrink-0 rounded-full px-1.5 py-px font-medium transition-colors duration-500', tone.badge)}>{card.status}</span>
      </div>
      {card.tone === 'running' && <span aria-hidden className="live-shimmer absolute inset-x-0 bottom-0 h-0.5 bg-violet-400/40" />}
      {card.tone === 'failed' && card.detail && (
        <p className="mt-1.5 line-clamp-2 font-mono text-[10.5px] leading-snug text-red-300">{card.detail}</p>
      )}
    </div>
  )
}

/**
 * Demo kanban of the architect's breakdown: Backlog → In Dev → CI → Done (Review/QA only
 * when occupied). Shows "Architect is planning…" until the first task exists.
 */
export function TaskBoard({
  tasks,
  steps,
  prNumber,
  ready = true,
}: {
  tasks: Task[]
  steps: JourneyStep[] | undefined
  prNumber: number | null
  /** False while the tasks are still loading, so the first data does not animate in. */
  ready?: boolean
}) {
  const pr = prCard(steps, prNumber)
  const columns = boardColumns(tasks, pr ? [pr.column] : [])
  const plan = steps?.find((s) => s.stage === 'plan')
  const planning = tasks.length === 0 && plan?.status === 'active'
  const done = tasks.filter((t) => t.status === 'done').length
  const root = useRef<HTMLDivElement>(null)
  const signature = [
    ...tasks.map((t) => `${t.id}:${t.status}:${t.assignee ?? ''}`),
    pr ? `${pr.column}:${pr.tone}:${pr.status}` : '',
  ].join('|')
  useCardFlip(root, signature, ready && steps !== undefined)

  return (
    <section className="rounded-lg border border-border p-4" aria-labelledby="task-board">
      <div className="mb-3 flex items-baseline gap-3">
        <h2 id="task-board" className="text-sm font-semibold text-foreground">
          Architect’s breakdown
        </h2>
        <span className="text-xs text-muted-foreground">
          {tasks.length === 0
            ? planning
              ? 'splitting the story into tasks'
              : 'no tasks yet'
            : `${tasks.length} tasks · ${done} done`}
        </span>
        {tasks.length > 0 && (
          <div className="ml-auto h-1.5 w-48 overflow-hidden rounded-full bg-zinc-800">
            <div
              className="demo-smooth h-full rounded-full bg-teal-400 transition-[width] duration-1000 ease-out"
              style={{ width: `${(done / tasks.length) * 100}%` }}
            />
          </div>
        )}
      </div>
      <div ref={root} className="relative">
        <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}>
          {columns.map((column) => (
            <div key={column.id} className="min-w-0 rounded-md bg-zinc-950/70 p-2">
              <div className="mb-2 flex items-center gap-2 px-1">
                <span className={cn('h-2 w-2 rounded-full', columnAccent[column.id])} />
                <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">{column.label}</span>
                <span className="ml-auto font-mono text-[11px] text-zinc-500">
                  {column.tasks.length + (pr?.column === column.id ? 1 : 0)}
                </span>
              </div>
              {/* Fixed height for the whole scene: cards moving between columns never resize the page. */}
              <div className="flex min-h-[352px] flex-col gap-2">
                {pr?.column === column.id && column.id !== 'done' && <PullRequestCard card={pr} />}
                {column.tasks.map((task) => (
                  <TaskCard key={task.id} task={task} />
                ))}
                {pr?.column === column.id && column.id === 'done' && <PullRequestCard card={pr} />}
              </div>
            </div>
          ))}
        </div>
        {ready && tasks.length === 0 && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center">
            <div className="flex items-center gap-2 rounded-full border border-teal-500/40 bg-zinc-950/90 px-4 py-2 text-sm text-teal-200 shadow-lg">
              <Sparkles className={cn('h-4 w-4', planning && 'animate-pulse')} />
              {planning ? 'Architect is planning…' : 'Waiting for the architect'}
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
