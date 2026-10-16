import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, FileText, Folder, GitBranch } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { ChangedFile, ChangedFileStatus, JourneyRepository } from '@/types/api'
import {
  changeDiff,
  collapsedFolders,
  headLabel,
  omittedChangesMessage,
  repositoryInterval,
  repositoryTreeRows,
  repositoryUnavailableMessage,
  visibleTreeRows,
  type DiffRowKind,
} from '@/pages/repositoryView'

const statusStyles: Record<ChangedFileStatus, string> = {
  added: 'bg-emerald-900/70 text-emerald-200',
  removed: 'bg-red-900/60 text-red-200',
  modified: 'bg-amber-900/60 text-amber-200',
  renamed: 'bg-sky-900/60 text-sky-200',
  copied: 'bg-sky-900/60 text-sky-200',
  changed: 'bg-amber-900/60 text-amber-200',
  unchanged: 'bg-zinc-800 text-zinc-300',
}

const rowStyles: Record<DiffRowKind, string> = {
  add: 'bg-emerald-500/10 text-emerald-200',
  del: 'bg-red-500/10 text-red-200',
  ctx: 'text-zinc-300',
  hunk: 'bg-sky-500/5 text-sky-400/80',
  note: 'text-zinc-500 italic',
}

const signs: Record<'add' | 'del' | 'ctx', string> = { add: '+', del: '-', ctx: ' ' }

function Counts({ additions, deletions }: { additions: number; deletions: number }) {
  return (
    <span className="shrink-0 font-mono text-[10.5px]">
      <span className="text-emerald-400">+{additions}</span> <span className="text-red-400">-{deletions}</span>
    </span>
  )
}

function DiffView({ change }: { change: ChangedFile }) {
  const diff = changeDiff(change)
  return (
    <div className="flex min-h-0 min-w-0 flex-col">
      <div className="flex min-w-0 items-center gap-2 border-b border-zinc-800 px-3 py-1.5 text-xs">
        <span className="min-w-0 truncate font-mono text-zinc-100">{change.path}</span>
        <span className={cn('shrink-0 rounded px-1.5 py-px text-[10.5px]', statusStyles[change.status])}>{change.status}</span>
        <Counts additions={change.additions} deletions={change.deletions} />
      </div>
      {diff.notice && <p className="border-b border-zinc-800 px-3 py-2 text-xs text-amber-300">{diff.notice}</p>}
      {diff.rows && (
        <div className="max-h-[480px] overflow-y-auto overflow-x-hidden py-1 font-mono text-[12px] leading-[1.6]">
          {diff.rows.map((row, i) =>
            row.kind === 'hunk' || row.kind === 'note' ? (
              <div key={i} className={cn('px-3 py-0.5', rowStyles[row.kind])}>
                {row.text}
              </div>
            ) : (
              <div key={i} className={cn('grid grid-cols-[40px_40px_14px_minmax(0,1fr)]', rowStyles[row.kind])}>
                <span className="select-none pr-1 text-right tabular-nums text-zinc-600">{row.old ?? ''}</span>
                <span className="select-none pr-1 text-right tabular-nums text-zinc-600">{row.new ?? ''}</span>
                <span
                  className={cn(
                    'select-none text-center',
                    row.kind === 'add' && 'text-emerald-400',
                    row.kind === 'del' && 'text-red-400',
                  )}
                >
                  {signs[row.kind]}
                </span>
                <span className="whitespace-pre-wrap break-words pr-3">{row.text || ' '}</span>
              </div>
            ),
          )}
        </div>
      )}
    </div>
  )
}

function RepositoryBody({ repo }: { repo: JourneyRepository }) {
  const [picked, setPicked] = useState<string | null>(null)
  const [toggled, setToggled] = useState<ReadonlySet<string>>(() => new Set())
  const rows = useMemo(() => repositoryTreeRows(repo.files, repo.changes), [repo.files, repo.changes])
  const collapsed = collapsedFolders(rows, toggled)
  const visible = visibleTreeRows(rows, collapsed)
  const selected = repo.changes.find((change) => change.path === picked) ?? repo.changes[0] ?? null
  const omitted = omittedChangesMessage(repo.omitted_changes, repo.omitted_changes_exact)

  const toggle = (path: string) =>
    setToggled((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  return (
    <>
      {omitted && <p className="border-b border-zinc-800 px-3 py-2 text-xs text-zinc-400">{omitted}</p>}
      <div className="grid lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
        <ul
          className="max-h-[520px] overflow-y-auto overflow-x-hidden border-b border-zinc-800 py-1.5 font-mono text-[12px] lg:border-b-0 lg:border-r"
          aria-label="Files"
        >
          {rows.length === 0 && <li className="px-3 py-1 text-zinc-500">The tree at this head is empty.</li>}
          {visible.map((row) => {
            const change = row.change
            const open = row.is_dir && !collapsed.has(row.path)
            return (
              <li key={row.path}>
                <button
                  type="button"
                  disabled={!row.is_dir && !change}
                  aria-expanded={row.is_dir ? open : undefined}
                  aria-current={change !== null && change === selected ? 'true' : undefined}
                  onClick={() => (row.is_dir ? toggle(row.path) : setPicked(row.path))}
                  className={cn(
                    'flex w-full items-center gap-1.5 py-[3px] pr-2 text-left',
                    change !== null && change === selected
                      ? 'bg-teal-500/10 text-teal-100'
                      : row.is_dir || change
                        ? 'text-zinc-300 hover:bg-zinc-800/60'
                        : 'text-zinc-500',
                  )}
                  style={{ paddingLeft: 8 + row.depth * 14 }}
                >
                  {row.is_dir ? (
                    <>
                      {open ? (
                        <ChevronDown className="h-3 w-3 shrink-0 text-zinc-500" />
                      ) : (
                        <ChevronRight className="h-3 w-3 shrink-0 text-zinc-500" />
                      )}
                      <Folder className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
                    </>
                  ) : (
                    <FileText className="ml-[18px] h-3.5 w-3.5 shrink-0 text-zinc-500" />
                  )}
                  <span className={cn('min-w-0 truncate', change?.status === 'removed' && 'line-through')}>{row.name}</span>
                  {row.is_dir && row.changed > 0 && !open && (
                    <span className="ml-auto shrink-0 text-[10.5px] text-amber-300">{row.changed} changed</span>
                  )}
                  {change && (
                    <>
                      <span className={cn('ml-auto shrink-0 rounded px-1 text-[10px] leading-4', statusStyles[change.status])}>
                        {change.status}
                      </span>
                      <Counts additions={change.additions} deletions={change.deletions} />
                    </>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
        {selected ? (
          <DiffView key={selected.path} change={selected} />
        ) : (
          <p className="p-4 text-sm text-zinc-500">No changes on this head yet.</p>
        )}
      </div>
    </>
  )
}

/** The story's product repository at its head: the file tree with the story's changes marked, and one change's diff. */
export function RepositoryPanel({ storyId, live }: { storyId: string; live: boolean }) {
  const repository = useQuery({
    queryKey: ['admin-console-journey-repository', storyId],
    queryFn: () => api.get<JourneyRepository>(`/admin/v2/journeys/${storyId}/repository`),
    refetchInterval: repositoryInterval(live),
  })
  const { refetch } = repository
  // One more read once the story ends picks up its final head.
  const wasLive = useRef(live)
  useEffect(() => {
    if (wasLive.current && !live) void refetch()
    wasLive.current = live
  }, [live, refetch])

  const repo = repository.data
  const changed = repo?.changes ?? []
  const additions = changed.reduce((sum, change) => sum + change.additions, 0)
  const deletions = changed.reduce((sum, change) => sum + change.deletions, 0)

  return (
    <section aria-labelledby="repository-panel" className="overflow-hidden rounded-lg border border-zinc-800 bg-[#0b0d10]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-zinc-800 px-3 py-2">
        <GitBranch className="h-4 w-4 text-zinc-400" />
        <h2 id="repository-panel" className="text-sm font-semibold text-foreground">
          Repository
        </h2>
        {repo?.repository && (
          <span className="font-mono text-[11px] text-zinc-300">
            {repo.repository.owner}/{repo.repository.name}
          </span>
        )}
        {repo?.head && <span className="font-mono text-[11px] text-zinc-500">{headLabel(repo.head)}</span>}
        {changed.length > 0 && (
          <span className="font-mono text-[11px] text-zinc-400">
            {changed.length} changed · <Counts additions={additions} deletions={deletions} />
          </span>
        )}
      </div>
      {repository.isError && (
        <p role="alert" className="border-b border-zinc-800 px-3 py-2 text-sm text-red-400">
          Repository request failed.
        </p>
      )}
      {repo?.unavailable ? (
        <p role="alert" className="px-3 py-3 text-sm text-amber-300">
          {repositoryUnavailableMessage(repo.unavailable)}
        </p>
      ) : repo ? (
        <RepositoryBody repo={repo} />
      ) : (
        repository.isLoading && <p className="px-3 py-3 text-sm text-zinc-500">Loading…</p>
      )}
    </section>
  )
}
