import type { ChangedFile, RepositoryHead, RepositoryUnavailable, RepositoryUnavailableReason } from '../types/api'

/** How often the repository panel rereads a running story's head; each read costs GitHub API calls. */
export const REPOSITORY_POLL_MS = 30_000

/** The repository read's refetch interval: polls while the story runs, reads once otherwise. */
export function repositoryInterval(live: boolean): number | false {
  return live ? REPOSITORY_POLL_MS : false
}

const HEAD_SOURCES: Record<RepositoryHead['source'], string> = {
  pull_request: 'pull request',
  branch: 'branch',
}

/** The head as `source ref @ short-sha`. */
export function headLabel(head: RepositoryHead): string {
  return `${HEAD_SOURCES[head.source]} ${head.ref} @ ${head.sha.slice(0, 7)}`
}

export interface TreeRow {
  path: string
  name: string
  depth: number
  is_dir: boolean
  /** The story's change to this file; null for a folder or an unchanged file. */
  change: ChangedFile | null
  /** Changed files at or below this row. */
  changed: number
}

interface TreeNode {
  path: string
  name: string
  /** Null for a file. */
  children: Map<string, TreeNode> | null
}

/**
 * The head tree as pre-order rows, folders first and each level sorted by name.
 * A removed file is not in the head tree; it gets a row from its change.
 */
export function repositoryTreeRows(files: string[], changes: ChangedFile[]): TreeRow[] {
  const byPath = new Map(changes.map((change) => [change.path, change]))
  const root: TreeNode = { path: '', name: '', children: new Map() }
  for (const path of new Set([...files, ...byPath.keys()])) {
    const parts = path.split('/')
    let node = root
    parts.forEach((name, i) => {
      const children = node.children!
      let child = children.get(name)
      if (!child) {
        const isFile = i === parts.length - 1
        child = { path: parts.slice(0, i + 1).join('/'), name, children: isFile ? null : new Map() }
        children.set(name, child)
      }
      node = child
    })
  }

  const rows: TreeRow[] = []
  const walk = (node: TreeNode, depth: number): number => {
    const level = [...node.children!.values()].sort((x, y) =>
      Boolean(x.children) !== Boolean(y.children) ? (x.children ? -1 : 1) : x.name.localeCompare(y.name),
    )
    let total = 0
    for (const child of level) {
      if (child.children) {
        const row: TreeRow = { path: child.path, name: child.name, depth, is_dir: true, change: null, changed: 0 }
        rows.push(row)
        row.changed = walk(child, depth + 1)
        total += row.changed
      } else {
        const change = byPath.get(child.path) ?? null
        rows.push({ path: child.path, name: child.name, depth, is_dir: false, change, changed: change ? 1 : 0 })
        if (change) total += 1
      }
    }
    return total
  }
  walk(root, 0)
  return rows
}

/** Collapsed folders: one with no changed file below it starts collapsed, and a reader's toggle flips it. */
export function collapsedFolders(rows: TreeRow[], toggled: ReadonlySet<string>): Set<string> {
  return new Set(rows.filter((row) => row.is_dir && (row.changed === 0) !== toggled.has(row.path)).map((row) => row.path))
}

/** The rows a reader sees: everything below a collapsed folder is hidden. */
export function visibleTreeRows(rows: TreeRow[], collapsed: ReadonlySet<string>): TreeRow[] {
  const visible: TreeRow[] = []
  let hiddenUnder: string | null = null
  for (const row of rows) {
    if (hiddenUnder !== null && row.path.startsWith(`${hiddenUnder}/`)) continue
    hiddenUnder = row.is_dir && collapsed.has(row.path) ? row.path : null
    visible.push(row)
  }
  return visible
}

export type DiffRowKind = 'add' | 'del' | 'ctx' | 'hunk' | 'note'

export interface DiffRow {
  kind: DiffRowKind
  /** Line number in the old / new file, null where the side has no line. */
  old: number | null
  new: number | null
  text: string
}

/** Unified-diff hunks as rows with old/new line numbers; a `\` line is a note between them. */
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
    } else if (raw.startsWith('\\')) {
      rows.push({ kind: 'note', old: null, new: null, text: raw.slice(1).trim() })
    } else if (raw !== '') {
      rows.push({ kind: 'ctx', old: a++, new: b++, text: raw.slice(1) })
    }
  }
  return rows
}

const NO_PATCH_MESSAGE = 'diff not available (binary or too large)'

const TRUNCATED_PATCH_MESSAGE = 'This diff was cut at the read limit; the rest is not shown.'

export interface ChangeDiff {
  /** Null when GitHub rendered no patch. */
  rows: DiffRow[] | null
  /** Shown above the rows: why there are none, or that they were cut. */
  notice: string | null
}

export function changeDiff(change: ChangedFile): ChangeDiff {
  if (change.patch === null) return { rows: null, notice: NO_PATCH_MESSAGE }
  return { rows: parsePatch(change.patch), notice: change.truncated ? TRUNCATED_PATCH_MESSAGE : null }
}

/** The notice for changed files the read left out, or null when it returned them all. */
export function omittedChangesMessage(omitted: number, exact: boolean): string | null {
  const files = omitted === 1 ? 'file' : 'files'
  if (!exact) return `at least ${omitted} more ${files} not shown`
  return omitted === 0 ? null : `${omitted} more ${files} not shown`
}

const UNAVAILABLE_MESSAGES: Record<RepositoryUnavailableReason, (detail: string | null) => string> = {
  no_repository: () => 'This story has no product repository.',
  no_story_branch: () => 'No story branch yet: nothing has been pushed for this story.',
  github_unavailable: (detail) => (detail ? `GitHub is unavailable (${detail}).` : 'GitHub is unavailable.'),
}

export function repositoryUnavailableMessage(unavailable: RepositoryUnavailable): string {
  return UNAVAILABLE_MESSAGES[unavailable.reason](unavailable.detail)
}
