"""A story's product repository at its head and the diff of its own changes, read from GitHub.

The repository is the project's primary ``Repository``. The head is the story's pull request
head, or the head of its branch ``story/{story_id}`` before a pull request exists.
"""

import asyncio

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.repository import RepositoryRole
from shared.models import Repository, Story

from .github_repo_url import parse_github_repo_url
from .schemas.admin_console import (
    ChangedFile,
    JourneyRepository,
    RepositoryHead,
    RepositoryHeadSource,
    RepositoryRef,
    RepositoryUnavailable,
    RepositoryUnavailableReason,
)

logger = structlog.get_logger()

#: The most changed files one read returns, in GitHub's order.
CHANGED_FILE_LIMIT = 100

#: The longest patch one changed file carries, in characters.
PATCH_LENGTH_LIMIT = 20_000

#: How long all GitHub calls of one read may take together.
GITHUB_READ_TIMEOUT_SECONDS = 20.0

#: The most files GitHub lists for one comparison; a list this long may have been cut.
GITHUB_COMPARE_FILE_CAP = 300


async def _story_comparison(
    github: GitHubAppClient, owner: str, repo: str, story: Story
) -> tuple[str, RepositoryHead, int | None] | None:
    """The base to compare from, the story's head and its exact changed file count if known.

    ``None`` when the story has no head. The repository is read first, so a repository GitHub
    does not serve fails the read instead of passing for a missing story branch. A pull request
    compares its ``base.sha`` with its ``head.sha``, open or merged; a branch without a pull
    request compares the default branch with the branch head.
    """
    default_branch = (await github.get_repo(owner, repo)).default_branch
    if story.pr_number is not None:
        pr = await github.get_pull_request(owner, repo, story.pr_number)
        head = RepositoryHead(
            source=RepositoryHeadSource.PULL_REQUEST, ref=pr["head"]["ref"], sha=pr["head"]["sha"]
        )
        return pr["base"]["sha"], head, pr["changed_files"]
    branch = f"story/{story.id}"
    sha = await github.get_ref_sha(owner, repo, f"heads/{branch}")
    if sha is None:
        return None
    head = RepositoryHead(source=RepositoryHeadSource.BRANCH, ref=branch, sha=sha)
    return default_branch, head, None


def _changed_files(
    files: list[dict], changed_total: int | None
) -> tuple[list[ChangedFile], int, bool]:
    """The comparison files within the file and patch bounds, how many were omitted, and whether
    that count is exact.

    Without ``changed_total`` a list at GitHub's cap may have been cut: its count is a lower bound.
    """
    kept = files[:CHANGED_FILE_LIMIT]
    changes = []
    for item in kept:
        patch = item.get("patch")
        truncated = patch is not None and len(patch) > PATCH_LENGTH_LIMIT
        changes.append(
            ChangedFile(
                path=item["filename"],
                status=item["status"],
                additions=item["additions"],
                deletions=item["deletions"],
                patch=patch[:PATCH_LENGTH_LIMIT] if truncated else patch,
                truncated=truncated,
            )
        )
    if changed_total is not None:
        return changes, changed_total - len(kept), True
    return changes, len(files) - len(kept), len(files) < GITHUB_COMPARE_FILE_CAP


def _unavailable(
    repository: RepositoryRef | None, reason: RepositoryUnavailableReason, detail: str | None = None
) -> JourneyRepository:
    return JourneyRepository(
        repository=repository,
        head=None,
        files=[],
        changes=[],
        omitted_changes=0,
        omitted_changes_exact=True,
        unavailable=RepositoryUnavailable(reason=reason, detail=detail),
    )


async def _read_github(owner: str, repo: str, story: Story) -> JourneyRepository:
    reference = RepositoryRef(owner=owner, name=repo)
    async with GitHubAppClient() as github:
        comparison = await _story_comparison(github, owner, repo, story)
        if comparison is None:
            return _unavailable(reference, RepositoryUnavailableReason.NO_STORY_BRANCH)
        base, head, changed_total = comparison
        files = await github.list_repo_files_recursive(owner, repo, head.sha)
        compared = await github.compare_commits(owner, repo, base, head.sha)
        changes, omitted, omitted_exact = _changed_files(compared, changed_total)
    return JourneyRepository(
        repository=reference,
        head=head,
        files=files,
        changes=changes,
        omitted_changes=omitted,
        omitted_changes_exact=omitted_exact,
        unavailable=None,
    )


async def read_story_repository(db: AsyncSession, story_id: str) -> JourneyRepository | None:
    """The story's repository tree and diff at its head; ``None`` for an unknown story."""
    story = await db.get(Story, story_id)
    if story is None:
        return None
    repository = await db.scalar(
        select(Repository)
        .where(
            Repository.project_id == story.project_id,
            Repository.role == RepositoryRole.PRIMARY.value,
        )
        .order_by(Repository.id)
        .limit(1)
    )
    if repository is None:
        return _unavailable(None, RepositoryUnavailableReason.NO_REPOSITORY)
    owner, repo = parse_github_repo_url(repository.git_url)
    try:
        return await asyncio.wait_for(
            _read_github(owner, repo, story), timeout=GITHUB_READ_TIMEOUT_SECONDS
        )
    except httpx.HTTPStatusError as exc:
        detail = str(exc.response.status_code)
    except (httpx.HTTPError, TimeoutError) as exc:
        detail = type(exc).__name__
    logger.warning(
        "journey_repository_read_failed", story_id=story_id, owner=owner, repo=repo, detail=detail
    )
    return _unavailable(
        RepositoryRef(owner=owner, name=repo),
        RepositoryUnavailableReason.GITHUB_UNAVAILABLE,
        detail,
    )
