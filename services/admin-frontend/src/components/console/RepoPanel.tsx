import { useLayoutEffect, useRef, useState } from 'react'
import { FileCode2, FileText, Folder, GitBranch } from 'lucide-react'
import { cn } from '@/lib/utils'
import { logTime } from '@/lib/display'
import { parsePatch, treeRows, treeTotals } from '@/pages/liveFeed'
import type { RepoFileChange } from '@/types/api'
import { useJourneyLive } from '@/lib/useJourneyLive'
import { Split } from '@/components/ui/Split'

const rowStyles = {
  add: 'bg-emerald-500/10 text-emerald-200',
  del: 'bg-red-500/10 text-red-200',
  ctx: 'text-zinc-300',
  hunk: 'bg-sky-500/5 text-sky-400/80',
} as const

const sign = { add: '+', del: '-', ctx: ' ', hunk: '' } as const

/** One file's diff: line numbers, +/- gutter and green/red rows; wraps instead of scrolling sideways. */
export function DiffView({ change, className }: { change: RepoFileChange; className?: string }) {
  const rows = parsePatch(change.patch)
  return (
    <div className={cn('font-mono text-[12px] leading-[1.6]', className)}>
      {rows.map((row, i) =>
        row.kind === 'hunk' ? (
          <div key={i} className={cn('px-3 py-0.5', rowStyles.hunk)}>
            {row.text}
          </div>
        ) : (
          <div key={i} className={cn('grid grid-cols-[34px_34px_14px_minmax(0,1fr)]', rowStyles[row.kind])}>
            <span className="select-none pr-1 text-right tabular-nums text-zinc-600">{row.old ?? ''}</span>
            <span className="select-none pr-1 text-right tabular-nums text-zinc-600">{row.new ?? ''}</span>
            <span className={cn('select-none text-center', row.kind === 'add' ? 'text-emerald-400' : row.kind === 'del' ? 'text-red-400' : '')}>
              {sign[row.kind]}
            </span>
            <span className="whitespace-pre-wrap break-words pr-3">{row.text || ' '}</span>
          </div>
        ),
      )}
    </div>
  )
}

export function ChangeHeader({ change, className }: { change: RepoFileChange; className?: string }) {
  return (
    <div className={cn('flex min-w-0 items-center gap-2 text-xs', className)}>
      <FileCode2 className="h-3.5 w-3.5 shrink-0 text-zinc-400" />
      <span className="truncate font-mono text-zinc-100">{change.path}</span>
      <span
        className={cn(
          'shrink-0 rounded px-1.5 py-px text-[10.5px] font-medium',
          change.status === 'added' ? 'bg-emerald-950 text-emerald-300' : 'bg-amber-950 text-amber-300',
        )}
      >
        {change.status === 'added' ? 'new file' : 'modified'}
      </span>
      <span className="shrink-0 font-mono text-emerald-400">+{change.additions}</span>
      <span className="shrink-0 font-mono text-red-400">−{change.deletions}</span>
    </div>
  )
}

/**
 * Demo build: the product repository as the agents build it — the tree grows file by file,
 * and the right side shows the latest write as a diff (or the file picked in the tree).
 */
export function RepoPanel({ storyId, className }: { storyId: string | undefined; className?: string }) {
  const [picked, setPicked] = useState<string | null>(null)
  const { data } = useJourneyLive(storyId, undefined, picked)
  const files = data?.files ?? []
  const totals = treeTotals(files)
  const focus = data?.focus ?? null

  // Rows present when the panel opened are shown as is; later files fade in.
  const [known, setKnown] = useState<Set<string> | null>(null)
  if (known === null && data) setKnown(new Set(files.map((f) => f.path)))

  const diffBox = useRef<HTMLDivElement>(null)
  const focusKey = focus ? `${focus.path}@${focus.at ?? ''}` : ''
  useLayoutEffect(() => {
    diffBox.current?.scrollTo({ top: 0 })
  }, [focusKey])

  return (
    <section aria-labelledby="repo-panel" className={cn('overflow-hidden rounded-lg border border-zinc-800 bg-[#0b0d10]', className)}>
      <div className="flex items-center gap-2 border-b border-zinc-800 px-3 py-2">
        <GitBranch className="h-4 w-4 text-zinc-400" />
        <h2 id="repo-panel" className="text-sm font-semibold text-foreground">
          Repository
        </h2>
        <span className="font-mono text-[11px] text-zinc-500">{data?.repo ?? ''}</span>
        {totals.files > 0 && (
          <span className="ml-3 font-mono text-[11px] text-zinc-400">
            {totals.files} files · <span className="text-emerald-400">+{totals.additions}</span>{' '}
            <span className="text-red-400">−{totals.deletions}</span> since scaffold
          </span>
        )}
        {picked && (
          <button
            type="button"
            onClick={() => setPicked(null)}
            className="ml-auto rounded border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-300 hover:border-teal-400/60"
          >
            Follow latest change
          </button>
        )}
      </div>
      {/* file tree | diff, split by a draggable handle (stacked on narrow screens) */}
      <Split
        id="repo"
        defaultLeft={26}
        minLeft={14}
        minRight={35}
        className="h-[420px]"
        stackedClassName="gap-0 [&>div:first-child]:max-h-[220px] [&>div:first-child]:overflow-y-auto [&>div:first-child]:border-b [&>div:first-child]:border-zinc-800 [&>div:last-child]:h-[420px]"
        left={
                  <ul className="h-full overflow-y-auto overflow-x-hidden py-1.5 font-mono text-[12px]" aria-label="Files">
                    {files.length === 0 && <li className="px-3 py-1 text-zinc-500">Empty — waiting for the scaffold</li>}
                    {treeRows(files).map((row) => {
                      const active = !row.is_dir && focus?.path === row.path
                      return (
                        <li key={row.path} className={cn(known !== null && !known.has(row.path) && 'log-enter')}>
                          <button
                            type="button"
                            disabled={row.is_dir}
                            onClick={() => setPicked(row.path)}
                            className={cn(
                              'flex w-full items-center gap-1.5 py-[3px] pr-2 text-left',
                              active ? 'bg-teal-500/10 text-teal-100' : row.is_dir ? 'text-zinc-400' : 'text-zinc-300 hover:bg-zinc-800/60',
                            )}
                            style={{ paddingLeft: 10 + row.depth * 14 }}
                          >
                            {row.is_dir ? (
                              <Folder className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
                            ) : (
                              <FileText className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
                            )}
                            <span className="min-w-0 truncate">{row.name}</span>
                            {row.mark && (
                              <span
                                className={cn(
                                  'shrink-0 rounded px-1 text-[10px] leading-4',
                                  row.mark === 'new' ? 'bg-emerald-900/70 text-emerald-200' : 'bg-amber-900/60 text-amber-200',
                                )}
                              >
                                {row.mark === 'new' ? 'new' : 'edited'}
                              </span>
                            )}
                            {!row.is_dir && (row.additions ?? 0) + (row.deletions ?? 0) > 0 && (
                              <span className="ml-auto shrink-0 text-[10.5px]">
                                <span className="text-emerald-500">+{row.additions}</span>
                                {(row.deletions ?? 0) > 0 && <span className="ml-1 text-red-500">−{row.deletions}</span>}
                              </span>
                            )}
                          </button>
                        </li>
                      )
                    })}
                  </ul>
        }
        right={
                  <div className="flex h-full min-h-0 min-w-0 flex-col">
                    {focus ? (
                      <>
                        <div className="flex items-center gap-3 border-b border-zinc-800 px-3 py-1.5">
                          <ChangeHeader change={focus} className="min-w-0 flex-1" />
                          {focus.author && (
                            <span className="shrink-0 text-[11px] text-zinc-500">
                              {focus.author}
                              {focus.at && <> · {logTime(focus.at)}</>}
                            </span>
                          )}
                        </div>
                        <div ref={diffBox} className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden py-1">
                          <DiffView key={focusKey} change={focus} className="log-enter" />
                        </div>
                      </>
                    ) : (
                      <p className="p-4 text-sm text-zinc-500">No code written yet — the first diff shows up here.</p>
                    )}
                  </div>
        }
      />
    </section>
  )
}

/** Task page: every file the task changed, as one stacked diff. */
export function TaskChanges({ changes }: { changes: RepoFileChange[] }) {
  if (changes.length === 0) return <p className="text-sm text-zinc-500">No changes yet.</p>
  return (
    <div className="space-y-3">
      {changes.map((change) => (
        <div key={change.path} className="overflow-hidden rounded-md border border-zinc-800 bg-[#0b0d10]">
          <ChangeHeader change={change} className="border-b border-zinc-800 px-3 py-1.5" />
          <DiffView change={change} className="py-1" />
        </div>
      ))}
    </div>
  )
}
