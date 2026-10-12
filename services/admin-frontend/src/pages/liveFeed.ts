/** Demo live log and repository panels: pure helpers, no React, no clock reads. */
import type { LiveLogLine, RepoTreeEntry } from '../types/api'

const TAGS: Record<string, string> = {
  'GitHub CI': 'ci',
  Scaffolder: 'scaffold',
  Deployer: 'deploy',
}

/** Short terminal-style tag for a log source: "Developer 2" → "dev 2", "GitHub CI" → "ci". */
export function sourceTag(source: string): string {
  if (TAGS[source]) return TAGS[source]
  return source.replace(/^Developer\b/, 'dev').toLowerCase()
}

export type TagTone = 'architect' | 'dev' | 'ci' | 'ship' | 'brimos'

export function tagTone(source: string): TagTone {
  if (source.startsWith('Developer')) return 'dev'
  if (source === 'GitHub CI') return 'ci'
  if (source === 'Deployer' || source === 'QA') return 'ship'
  if (source === 'Brimos') return 'brimos'
  return 'architect'
}

/** Lines newer than the ones the panel opened with: those fade in, the rest are shown as is. */
export function freshLines(lines: LiveLogLine[], baseline: number): LiveLogLine[] {
  return lines.filter((l) => l.n > baseline)
}

export type DiffRowKind = 'add' | 'del' | 'ctx' | 'hunk'

export interface DiffRow {
  kind: DiffRowKind
  /** Line number in the old / new file, null where the side has no line. */
  old: number | null
  new: number | null
  text: string
}

/** Unified-diff hunks → rows with old/new line numbers. */
export function parsePatch(patch: string): DiffRow[] {
  const rows: DiffRow[] = []
  let a = 0
  let b = 0
  for (const raw of patch.split('\n')) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw)
    if (hunk) {
      a = Number(hunk[1])
      b = Number(hunk[2])
      rows.push({ kind: 'hunk', old: null, new: null, text: raw })
    } else if (raw.startsWith('+')) {
      rows.push({ kind: 'add', old: null, new: b++, text: raw.slice(1) })
    } else if (raw.startsWith('-')) {
      rows.push({ kind: 'del', old: a++, new: null, text: raw.slice(1) })
    } else if (raw !== '' || rows.length > 0) {
      rows.push({ kind: 'ctx', old: a++, new: b++, text: raw.slice(1) })
    }
  }
  return rows
}

export interface TreeRow extends RepoTreeEntry {
  name: string
  depth: number
}

/** Flat, always-expanded tree: folders first, then files, each level sorted by name. */
export function treeRows(entries: RepoTreeEntry[]): TreeRow[] {
  const children = new Map<string, RepoTreeEntry[]>()
  for (const e of entries) {
    const parent = e.path.includes('/') ? e.path.slice(0, e.path.lastIndexOf('/')) : ''
    children.set(parent, [...(children.get(parent) ?? []), e])
  }
  const out: TreeRow[] = []
  const walk = (dir: string, depth: number) => {
    const level = [...(children.get(dir) ?? [])].sort((x, y) =>
      x.is_dir !== y.is_dir ? (x.is_dir ? -1 : 1) : x.path.localeCompare(y.path),
    )
    for (const e of level) {
      out.push({ ...e, name: e.path.slice(e.path.lastIndexOf('/') + 1), depth })
      if (e.is_dir) walk(e.path, depth + 1)
    }
  }
  walk('', 0)
  return out
}

/** Totals for the tree header: files and +/- the pull request adds over the scaffold. */
export function treeTotals(entries: RepoTreeEntry[]): { files: number; additions: number; deletions: number } {
  let files = 0
  let additions = 0
  let deletions = 0
  for (const e of entries) {
    if (e.is_dir) continue
    files += 1
    additions += e.additions ?? 0
    deletions += e.deletions ?? 0
  }
  return { files, additions, deletions }
}
