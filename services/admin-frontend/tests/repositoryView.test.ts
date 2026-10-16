import assert from 'node:assert/strict'
import test from 'node:test'

import type { ChangedFile, RepositoryUnavailableReason } from '../src/types/api.ts'
import {
  changeDiff,
  collapsedFolders,
  headLabel,
  omittedChangesMessage,
  parsePatch,
  repositoryInterval,
  repositoryTreeRows,
  repositoryUnavailableMessage,
  visibleTreeRows,
} from '../src/pages/repositoryView.ts'

function change(path: string, overrides: Partial<ChangedFile> = {}): ChangedFile {
  return { path, status: 'modified', additions: 1, deletions: 1, patch: '@@ -1 +1 @@\n-a\n+b', truncated: false, ...overrides }
}

const FILES = ['README.md', 'bot/handlers/goal.py', 'bot/streak.py', 'docs/guide.md', 'pyproject.toml']

test('the tree nests folders first, by name, and marks only changed files', () => {
  const rows = repositoryTreeRows(FILES, [
    change('bot/streak.py', { additions: 9, deletions: 3 }),
    change('bot/handlers/goal.py', { status: 'added', additions: 18, deletions: 0 }),
  ])
  assert.deepEqual(
    rows.map((r) => `${r.depth}:${r.name}${r.is_dir ? '/' : ''}`),
    ['0:bot/', '1:handlers/', '2:goal.py', '1:streak.py', '0:docs/', '1:guide.md', '0:pyproject.toml', '0:README.md'],
  )
  const byPath = new Map(rows.map((r) => [r.path, r]))
  assert.deepEqual(
    [byPath.get('bot/handlers/goal.py')?.change?.status, byPath.get('bot/handlers/goal.py')?.change?.additions],
    ['added', 18],
  )
  assert.deepEqual(
    [byPath.get('bot/streak.py')?.change?.additions, byPath.get('bot/streak.py')?.change?.deletions],
    [9, 3],
  )
  assert.equal(byPath.get('README.md')?.change, null)
  assert.deepEqual(
    ['bot', 'bot/handlers', 'docs'].map((p) => byPath.get(p)?.changed),
    [2, 1, 0],
  )
})

test('a removed file is in the tree from its change, not from the head', () => {
  const rows = repositoryTreeRows(['app.py'], [change('legacy/old.py', { status: 'removed', additions: 0, deletions: 40 })])
  assert.deepEqual(
    rows.map((r) => [r.path, r.change?.status ?? null]),
    [['legacy', null], ['legacy/old.py', 'removed'], ['app.py', null]],
  )
})

test('unchanged folders start collapsed, a toggle flips a folder, and collapsed folders hide what is below', () => {
  const rows = repositoryTreeRows(FILES, [change('bot/streak.py')])
  const initial = collapsedFolders(rows, new Set())
  assert.deepEqual([...initial].sort(), ['bot/handlers', 'docs'])
  assert.deepEqual(
    visibleTreeRows(rows, initial).map((r) => r.path),
    ['bot', 'bot/handlers', 'bot/streak.py', 'docs', 'pyproject.toml', 'README.md'],
  )

  const toggled = collapsedFolders(rows, new Set(['bot', 'docs']))
  assert.deepEqual([...toggled].sort(), ['bot', 'bot/handlers'])
  assert.deepEqual(
    visibleTreeRows(rows, toggled).map((r) => r.path),
    ['bot', 'docs', 'docs/guide.md', 'pyproject.toml', 'README.md'],
  )
})

test('a patch parses into hunks, numbered additions, removals and context', () => {
  const rows = parsePatch('@@ -3,3 +3,4 @@ def streak():\n ctx\n-    day = now.date()\n+    day = local_day(user, now)\n+    log(day)\n tail')
  assert.deepEqual(
    rows.map((r) => [r.kind, r.old, r.new]),
    [['hunk', null, null], ['ctx', 3, 3], ['del', 4, null], ['add', null, 4], ['add', null, 5], ['ctx', 5, 6]],
  )
  assert.equal(rows[2].text, '    day = now.date()')
  assert.equal(rows[3].text, '    day = local_day(user, now)')
})

test('a second hunk restarts the numbering and a no-newline marker is a note', () => {
  const rows = parsePatch('@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n@@ -10,2 +10,2 @@\n x\n-y\n+z')
  assert.deepEqual(
    rows.map((r) => [r.kind, r.old, r.new]),
    [
      ['hunk', null, null], ['del', 1, null], ['add', null, 1], ['note', null, null],
      ['hunk', null, null], ['ctx', 10, 10], ['del', 11, null], ['add', null, 11],
    ],
  )
  assert.equal(rows[3].text, 'No newline at end of file')
})

test('a null patch has no rows and says the diff is not available', () => {
  assert.deepEqual(changeDiff(change('logo.png', { patch: null })), {
    rows: null,
    notice: 'diff not available (binary or too large)',
  })
})

test('a truncated patch keeps its rows and says it was cut', () => {
  const truncated = changeDiff(change('big.py', { truncated: true }))
  assert.equal(truncated.rows?.length, 3)
  assert.match(truncated.notice ?? '', /cut/)
  assert.equal(changeDiff(change('small.py')).notice, null)
})

test('omitted changes read as an exact count or a lower bound', () => {
  assert.equal(omittedChangesMessage(0, true), null)
  assert.equal(omittedChangesMessage(1, true), '1 more file not shown')
  assert.equal(omittedChangesMessage(12, true), '12 more files not shown')
  assert.equal(omittedChangesMessage(200, false), 'at least 200 more files not shown')
})

test('each unavailable reason has its own message and GitHub failures carry the detail', () => {
  const reasons: RepositoryUnavailableReason[] = ['no_repository', 'no_story_branch', 'github_unavailable']
  const messages = reasons.map((reason) => repositoryUnavailableMessage({ reason, detail: null }))
  assert.ok(messages.every((m) => m.length > 0))
  assert.equal(new Set(messages).size, reasons.length)
  assert.match(repositoryUnavailableMessage({ reason: 'github_unavailable', detail: '502' }), /502/)
})

test('the head reads as source, ref and short sha', () => {
  const sha = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
  assert.equal(headLabel({ source: 'pull_request', ref: 'story/s-1', sha }), 'pull request story/s-1 @ a1b2c3d')
  assert.equal(headLabel({ source: 'branch', ref: 'story/s-1', sha }), 'branch story/s-1 @ a1b2c3d')
})

test('the repository polls at least every thirty seconds only while the story runs', () => {
  const interval = repositoryInterval(true)
  assert.equal(typeof interval, 'number')
  assert.ok((interval as number) >= 30_000)
  assert.equal(repositoryInterval(false), false)
})
