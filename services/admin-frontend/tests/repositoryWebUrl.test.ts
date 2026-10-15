import assert from 'node:assert/strict'
import test from 'node:test'

import { repositoryWebUrl } from '../src/lib/utils.ts'

test('an https git URL links to the repository page without .git', () => {
  assert.equal(repositoryWebUrl('https://github.com/org/repo.git'), 'https://github.com/org/repo')
})

test('the pre-scaffold placeholder has no link', () => {
  assert.equal(repositoryWebUrl('pending://p-abc'), null)
})

test('an empty or missing git URL has no link', () => {
  assert.equal(repositoryWebUrl(''), null)
  assert.equal(repositoryWebUrl(null), null)
})
