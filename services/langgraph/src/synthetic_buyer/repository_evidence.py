"""Read-only facts of the generated product's repository, through the existing GitHub App.

Deploy and install provenance cannot be judged from a typed status alone: the
pull request that merged, the `ci.yml` run whose `build-and-push` job published
the deployed commit's images, the separate `deploy.yml` run that placed them, the
commits an install published on top of the scaffold and the files engineering
changed after it are facts of the repository. This adapter reads them
with the platform's own `GitHubAppClient` installation token and GET requests
only; it never writes, and an unreadable fact is the caller's `unknown`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from shared.clients.github import GitHubAppClient

GITHUB_API = "https://api.github.com"
READ_TIMEOUT_SECONDS = 30


class RepositoryFactUnavailable(RuntimeError):  # noqa: N818 - an absent fact
    """A repository fact could not be read. Names the route, never a token."""


@dataclass(frozen=True)
class PullRequestFacts:
    number: int
    merged: bool
    head_sha: str | None
    base_sha: str | None
    merge_commit_sha: str | None


@dataclass(frozen=True)
class CompareFacts:
    base: str
    head: str
    #: GitHub's relation of head to base: `ahead`, `identical`, `behind`, `diverged`.
    status: str
    commits: tuple[str, ...]
    files: tuple[str, ...]


#: The generated product's publication workflow and the job that pushes its images
#: (`image_publication` in the scheduler reads the same run).
PUBLICATION_WORKFLOW = "ci.yml"
PUBLICATION_BRANCH = "main"


@dataclass(frozen=True)
class WorkflowRunFacts:
    id: int
    head_sha: str | None
    status: str | None
    conclusion: str | None
    path: str | None
    head_branch: str | None = None
    event: str | None = None


@dataclass(frozen=True)
class JobFacts:
    name: str
    status: str | None
    conclusion: str | None


class RepositoryFacts(Protocol):
    async def pull_request(self, repository: str, number: int) -> PullRequestFacts: ...

    async def compare(self, repository: str, base: str, head: str) -> CompareFacts: ...

    async def workflow_run(self, repository: str, run_id: int) -> WorkflowRunFacts: ...

    async def publication_runs(self, repository: str, commit: str) -> list[WorkflowRunFacts]: ...

    async def workflow_jobs(self, repository: str, run_id: int) -> list[JobFacts]: ...


def repository_name(git_url: str) -> str | None:
    """`owner/name` of a GitHub URL the scaffolder recorded, or None for anything else."""
    prefix = "https://github.com/"
    if not git_url.startswith(prefix):
        return None
    name = git_url.removeprefix(prefix).removesuffix(".git").strip("/")
    owner, _, repo = name.partition("/")
    return name if owner and repo and "/" not in repo else None


class GitHubRepositoryFacts:
    """The live adapter: the App's installation token for the owner, GET only."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get(self, repository: str, path: str) -> dict:
        owner = repository.split("/", 1)[0]
        async with GitHubAppClient() as app:
            token = await app.get_org_token(owner)
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        client = self._client or httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS)
        try:
            response = await client.get(f"{GITHUB_API}/repos/{repository}/{path}", headers=headers)
        except httpx.HTTPError as exc:
            raise RepositoryFactUnavailable(f"{path}: {type(exc).__name__}") from None
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code != httpx.codes.OK:
            raise RepositoryFactUnavailable(f"{path}: HTTP {response.status_code}")
        return response.json()

    async def pull_request(self, repository: str, number: int) -> PullRequestFacts:
        body = await self._get(repository, f"pulls/{number}")
        return PullRequestFacts(
            number=int(body["number"]),
            merged=bool(body.get("merged")),
            head_sha=(body.get("head") or {}).get("sha"),
            base_sha=(body.get("base") or {}).get("sha"),
            merge_commit_sha=body.get("merge_commit_sha"),
        )

    async def compare(self, repository: str, base: str, head: str) -> CompareFacts:
        body = await self._get(repository, f"compare/{base}...{head}")
        return CompareFacts(
            base=base,
            head=head,
            status=str(body.get("status")),
            commits=tuple(commit["sha"] for commit in body.get("commits") or []),
            files=tuple(item["filename"] for item in body.get("files") or []),
        )

    async def workflow_run(self, repository: str, run_id: int) -> WorkflowRunFacts:
        return _run_facts(await self._get(repository, f"actions/runs/{run_id}"))

    async def publication_runs(self, repository: str, commit: str) -> list[WorkflowRunFacts]:
        """Every `ci.yml` run GitHub has for *commit* on the default branch."""
        body = await self._get(
            repository,
            f"actions/workflows/{PUBLICATION_WORKFLOW}/runs"
            f"?branch={PUBLICATION_BRANCH}&head_sha={commit}&per_page=20",
        )
        return [_run_facts(run) for run in body.get("workflow_runs") or []]

    async def workflow_jobs(self, repository: str, run_id: int) -> list[JobFacts]:
        body = await self._get(repository, f"actions/runs/{run_id}/jobs?per_page=100")
        return [
            JobFacts(
                name=str(job.get("name") or ""),
                status=job.get("status"),
                conclusion=job.get("conclusion"),
            )
            for job in body.get("jobs") or []
        ]


def _run_facts(body: dict) -> WorkflowRunFacts:
    return WorkflowRunFacts(
        id=int(body["id"]),
        head_sha=body.get("head_sha"),
        status=body.get("status"),
        conclusion=body.get("conclusion"),
        path=body.get("path"),
        head_branch=body.get("head_branch"),
        event=body.get("event"),
    )
