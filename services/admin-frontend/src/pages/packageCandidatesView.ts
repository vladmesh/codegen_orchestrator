export class CandidateResponseError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'CandidateResponseError'
  }
}

interface PackageCandidateResponse {
  id: number
  project_id: string
  story_id: string
  repository_id: string
  source_task_id: string
  install_task_id: string
  path: string
  package_name: string
  package_distribution: string
  package_version: string
  commit: string
  core_version: string
  python_version: string
  tooling_commit: string
  capability: string
  rationale: string
  created_at: string
  updated_at: string
}

export interface PackageCandidateDisplay {
  id: number
  product: { id: string; href: string }
  story: { id: string; href: string }
  sourceTask: { id: string; href: string }
  installTask: { id: string; href: string }
  repositoryId: string
  path: string
  packageName: string
  packageDistribution: string
  packageVersion: string
  commit: string
  shortCommit: string
  coreVersion: string
  pythonVersion: string
  toolingCommit: string
  capability: string
  rationale: string
  createdAt: string
  updatedAt: string
}

function readObject(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new CandidateResponseError('Package candidate response is not an object.')
  }
  return value as Record<string, unknown>
}

function readString(record: Record<string, unknown>, field: string): string {
  const value = record[field]
  if (typeof value !== 'string') {
    throw new CandidateResponseError(`Package candidate response is missing ${field}.`)
  }
  return value
}

function readCandidate(value: unknown): PackageCandidateResponse {
  const record = readObject(value)
  if (typeof record.id !== 'number') {
    throw new CandidateResponseError('Package candidate response is missing id.')
  }
  return {
    id: record.id,
    project_id: readString(record, 'project_id'),
    story_id: readString(record, 'story_id'),
    repository_id: readString(record, 'repository_id'),
    source_task_id: readString(record, 'source_task_id'),
    install_task_id: readString(record, 'install_task_id'),
    path: readString(record, 'path'),
    package_name: readString(record, 'package_name'),
    package_distribution: readString(record, 'package_distribution'),
    package_version: readString(record, 'package_version'),
    commit: readString(record, 'commit'),
    core_version: readString(record, 'core_version'),
    python_version: readString(record, 'python_version'),
    tooling_commit: readString(record, 'tooling_commit'),
    capability: readString(record, 'capability'),
    rationale: readString(record, 'rationale'),
    created_at: readString(record, 'created_at'),
    updated_at: readString(record, 'updated_at'),
  }
}

function display(candidate: PackageCandidateResponse): PackageCandidateDisplay {
  return {
    id: candidate.id,
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
    shortCommit: candidate.commit.slice(0, 7),
    coreVersion: candidate.core_version,
    pythonVersion: candidate.python_version,
    toolingCommit: candidate.tooling_commit,
    capability: candidate.capability,
    rationale: candidate.rationale,
    createdAt: candidate.created_at,
    updatedAt: candidate.updated_at,
  }
}

/** Validates the candidate read API before the list renders any row. */
export function candidateList(response: unknown): PackageCandidateDisplay[] {
  if (!Array.isArray(response)) {
    throw new CandidateResponseError('Package candidate list response is not an array.')
  }
  return response.map((candidate) => display(readCandidate(candidate)))
}

/** Validates and derives every field the read-only detail view displays. */
export function candidateDetail(response: unknown): PackageCandidateDisplay {
  return display(readCandidate(response))
}
