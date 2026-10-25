import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CandidateResponseError,
  candidateDetail,
  candidateList,
} from '../src/pages/packageCandidatesView.ts'

const candidate = {
  id: 14,
  project_id: 'f75e7594-49b9-4a5c-a5b7-8b2f4a2dcf36',
  story_id: 'story-package',
  repository_id: 'repository-package',
  source_task_id: 'feature-package',
  install_task_id: 'install-package',
  path: 'packages/notes_digest',
  package_name: 'notes-digest',
  package_distribution: 'notes_digest',
  package_version: '0.1.0',
  commit: '1234567890abcdef1234567890abcdef12345678',
  core_version: '0.9.4',
  python_version: '3.13',
  tooling_commit: 'abcdef1234567890abcdef1234567890abcdef12',
  capability: 'Summarise product notes',
  rationale: 'The product needs one package that turns its notes into a daily digest.',
  created_at: '2026-10-25T12:00:00Z',
  updated_at: '2026-10-25T13:00:00Z',
}

test('a full candidate maps every display field', () => {
  const [display] = candidateList([candidate])

  assert.deepEqual(display, {
    id: 14,
    product: { id: candidate.project_id, href: `/projects/${candidate.project_id}` },
    story: { id: candidate.story_id, href: `/stories/${candidate.story_id}` },
    sourceTask: { id: candidate.source_task_id, href: `/tasks/${candidate.source_task_id}` },
    installTask: { id: candidate.install_task_id, href: `/tasks/${candidate.install_task_id}` },
    repositoryId: candidate.repository_id,
    path: candidate.path,
    packageName: candidate.package_name,
    packageDistribution: candidate.package_distribution,
    packageVersion: candidate.package_version,
    commit: candidate.commit,
    shortCommit: '1234567',
    coreVersion: candidate.core_version,
    pythonVersion: candidate.python_version,
    toolingCommit: candidate.tooling_commit,
    capability: candidate.capability,
    rationale: candidate.rationale,
    createdAt: candidate.created_at,
    updatedAt: candidate.updated_at,
  })
})

test('the detail keeps a long rationale whole', () => {
  const rationale = 'full rationale '.repeat(900)

  assert.equal(candidateDetail({ ...candidate, rationale }).rationale, rationale)
})

test('a malformed response is an error instead of a partial candidate', () => {
  const malformed: Record<string, unknown> = { ...candidate }
  delete malformed.rationale

  assert.throws(() => candidateList([malformed]), CandidateResponseError)
})

test('an empty response maps to the empty list state', () => {
  assert.deepEqual(candidateList([]), [])
})
