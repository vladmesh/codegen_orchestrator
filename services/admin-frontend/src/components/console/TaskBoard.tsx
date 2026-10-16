import { Link } from 'react-router'
import { Wrench } from 'lucide-react'
import { cn } from '@/lib/utils'
import { StatusBadge } from '@/components/ui/StatusBadge'
import type { TaskBoardColumn, TaskBoardEntry } from '@/types/api'
import { taskBoardColumns, type BoardCard } from '@/pages/taskBoard'

const columnAccent: Record<TaskBoardColumn, string> = {
  backlog: 'bg-zinc-500',
  in_development: 'bg-amber-400',
  ci: 'bg-violet-400',
  done: 'bg-teal-400',
}

function TaskCard({ card }: { card: BoardCard }) {
  return (
    <div
      className={cn(
        'relative overflow-hidden rounded-md border bg-zinc-900/80 py-2 pl-3 pr-2.5',
        card.fixMark ? 'border-red-500/60 bg-red-950/25' : 'border-zinc-800',
      )}
    >
      {card.fixMark && <span className="absolute inset-y-0 left-0 w-1 bg-red-500" />}
      <div className="flex items-start gap-1.5">
        {card.fixMark && (
          <span className="mt-px inline-flex shrink-0 items-center gap-0.5 rounded bg-red-600 px-1 py-px font-mono text-[9.5px] font-bold uppercase tracking-wider text-white">
            <Wrench className="h-2.5 w-2.5" />
            {card.fixMark}
          </span>
        )}
        <Link to={`/tasks/${card.taskId}`} className="min-w-0 break-words text-[13px] font-medium leading-snug text-foreground hover:underline">
          {card.title}
        </Link>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
        <span className="font-mono uppercase tracking-wider">{card.type}</span>
        <span className="font-mono">{card.worker}</span>
        <span>
          {card.attemptCount} {card.attemptCount === 1 ? 'attempt' : 'attempts'}
        </span>
        {card.unusualStatus && (
          <span className="ml-auto">
            <StatusBadge status={card.unusualStatus} />
          </span>
        )}
      </div>
      {card.attempts.length > 0 && (
        <ol className="mt-1.5 space-y-0.5 border-t border-zinc-800 pt-1.5 font-mono text-[10.5px] text-muted-foreground">
          {card.attempts.map((attempt) => (
            <li key={attempt.key} className="flex flex-wrap items-center gap-x-2">
              <span className="text-foreground">#{attempt.number}</span>
              <span>{attempt.status}</span>
              <span>{attempt.worker}</span>
              <span>{attempt.actor}</span>
              <span className="ml-auto tabular-nums">{attempt.duration}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/** The story's tasks in their four board columns, with each task's attempts. */
export function TaskBoard({ board, now }: { board: TaskBoardEntry[]; now: number }) {
  const columns = taskBoardColumns(board, now)
  const done = board.filter((entry) => entry.column === 'done').length
  return (
    <section className="min-w-0 rounded-lg border border-border p-4" aria-labelledby="task-board">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1.5">
        <h2 id="task-board" className="text-sm font-semibold text-foreground">
          Task board
        </h2>
        <span className="text-xs text-muted-foreground">
          {board.length === 0 ? 'no tasks yet' : `${board.length} tasks · ${done} done`}
        </span>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        {columns.map((column) => (
          <div key={column.id} className="min-w-0 rounded-md bg-zinc-950/70 p-2">
            <div className="mb-2 flex items-center gap-2 px-1">
              <span className={cn('h-2 w-2 rounded-full', columnAccent[column.id])} />
              <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">{column.label}</span>
              <span className="ml-auto font-mono text-[11px] text-zinc-500">{column.cards.length}</span>
            </div>
            <div className="flex min-h-[40px] flex-col gap-2">
              {column.cards.map((card) => (
                <TaskCard key={card.taskId} card={card} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
