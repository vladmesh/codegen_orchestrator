import assert from 'node:assert/strict'
import test from 'node:test'

import type { LiveLogLine } from '../src/types/api.ts'
import { freshLines, parsePatch, sourceTag, tagTone, treeRows, treeTotals } from '../src/pages/liveFeed.ts'

test('log sources get short terminal tags and tones', () => {
  assert.equal(sourceTag('Developer 2'), 'dev 2')
  assert.equal(sourceTag('GitHub CI'), 'ci')
  assert.equal(sourceTag('Architect'), 'architect')
  assert.equal(tagTone('Developer 3'), 'dev')
  assert.equal(tagTone('QA'), 'ship')
})

test('only lines after the baseline count as fresh', () => {
  const line = (n: number): LiveLogLine => ({ n, at: '', source: 'Architect', kind: 'out', text: '' })
  assert.deepEqual(freshLines([line(1), line(2), line(3)], 2).map((l) => l.n), [3])
  assert.deepEqual(freshLines([line(1)], 5), [])
})

test('parsePatch numbers both sides of a hunk', () => {
  const rows = parsePatch('@@ -3,3 +3,3 @@\n ctx\n-    day = now.date()\n+    day = local_day(user, now)\n tail')
  assert.deepEqual(rows.map((r) => [r.kind, r.old, r.new]), [
    ['hunk', null, null], ['ctx', 3, 3], ['del', 4, null], ['add', null, 4], ['ctx', 5, 5],
  ])
  assert.equal(rows[3].text, '    day = local_day(user, now)')
})

test('treeRows nests folders first and totals skip directories', () => {
  const entries = [
    { path: 'bot', is_dir: true, size: 0 },
    { path: 'bot/streak.py', is_dir: false, size: 10, additions: 9, deletions: 3 },
    { path: 'README.md', is_dir: false, size: 5 },
    { path: 'bot/handlers', is_dir: true, size: 0 },
    { path: 'bot/handlers/goal.py', is_dir: false, size: 7, additions: 18 },
  ]
  assert.deepEqual(treeRows(entries).map((r) => `${r.depth}:${r.name}`), [
    '0:bot', '1:handlers', '2:goal.py', '1:streak.py', '0:README.md',
  ])
  assert.deepEqual(treeTotals(entries), { files: 3, additions: 27, deletions: 3 })
})
