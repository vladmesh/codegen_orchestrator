"""Has the project's CI published the images of one exact commit yet.

The deploy path used to answer this by not asking. A merged PR dispatched a
deploy nine seconds later, the target pulled a mutable tag, and the run reported
a SHA over whatever bytes the registry happened to hold (paid run 33753667796).

The question is asked here, ahead of the deploy Run's creation, and that
position is the point: a Run that spends its own budget waiting for somebody
else's CI is a budget that has stopped meaning what it says, and a refusal that
costs no Run costs nothing at all. What the deploy itself then does is one
registry read of the exact references it resolved — see
`subgraphs/devops/image_gate.verify_published_images`.

The signal is the project's own `ci.yml` run for that commit on its default
branch, because that run's `build-and-push` job is what pushes the images: it is
the publication event, and reading it needs no second copy of which services a
generated project has. Nothing here builds, retriggers or repairs that CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import structlog

from shared.clients.github import GitHubAppClient

logger = structlog.get_logger(__name__)

CI_WORKFLOW = "ci.yml"
DEFAULT_BRANCH = "main"

# How long a merged commit is given to have its images published before the
# story is refused. Matches the deploy path's own bound
# (`image_gate.IMAGE_PUBLICATION_TIMEOUT_SECONDS`): the project's CI runs its
# tests and then builds and pushes one image per service from a cold cache, so
# the bound is minutes, not seconds. Measured from the merge, which is durable
# on GitHub, so the wait survives a scheduler restart without any state of ours.
IMAGE_PUBLICATION_TIMEOUT_SECONDS = 900

# Conclusions of a finished run. Only success publishes; every other conclusion
# is a run that ended without images for this commit, and no amount of waiting
# changes that, so it refuses at once instead of spending the bound.
_PUBLISHED_CONCLUSION = "success"
_COMPLETED_STATUS = "completed"


class ImagePublication(StrEnum):
    """What is known about one commit's images right now."""

    PUBLISHED = "published"
    PENDING = "pending"
    REFUSED = "refused"


@dataclass(frozen=True)
class PublicationVerdict:
    """One reading of the publication question, with what it was read from."""

    state: ImagePublication
    detail: str
    ci_run_id: int | None = None
    ci_conclusion: str | None = None
    ci_run_url: str | None = None

    def evidence(self) -> dict:
        """The typed record a story-level refusal carries."""
        return {
            "ci_run_id": self.ci_run_id,
            "ci_conclusion": self.ci_conclusion,
            "ci_run_url": self.ci_run_url,
            "detail": self.detail,
        }


def _elapsed_seconds(since: datetime, now: datetime) -> float:
    return (now - since).total_seconds()


def _expired(since: datetime | None, now: datetime, timeout_seconds: int) -> bool:
    if since is None:
        return False
    return _elapsed_seconds(since, now) >= timeout_seconds


async def image_publication_for_commit(
    github: GitHubAppClient,
    owner: str,
    repo: str,
    commit_sha: str,
    *,
    waiting_since: datetime | None,
    timeout_seconds: int = IMAGE_PUBLICATION_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> PublicationVerdict:
    """Whether this commit's images are published, still coming, or never coming.

    ``waiting_since`` is the moment the wait legitimately started — the merge —
    and the bound is measured from it, so the answer does not depend on how many
    times this has been asked. A ``None`` never expires, which is what a caller
    that has no such moment should get: pending, not refused.
    """
    moment = now or datetime.now(UTC)
    try:
        run = await github.get_latest_workflow_run(
            owner, repo, CI_WORKFLOW, DEFAULT_BRANCH, head_sha=commit_sha
        )
    except Exception as error:
        # Not asked is not an answer about the project. Stay pending and let the
        # bound decide, rather than refusing a story over a GitHub hiccup.
        logger.warning(
            "image_publication_unreadable",
            owner=owner,
            repo=repo,
            commit_sha=commit_sha,
            error_type=type(error).__name__,
        )
        if _expired(waiting_since, moment, timeout_seconds):
            return PublicationVerdict(
                state=ImagePublication.REFUSED,
                detail=(
                    f"the CI run for {commit_sha} could not be read within "
                    f"{timeout_seconds}s ({type(error).__name__})"
                ),
            )
        return PublicationVerdict(
            state=ImagePublication.PENDING,
            detail=f"the CI run for {commit_sha} could not be read ({type(error).__name__})",
        )

    if run is None:
        if _expired(waiting_since, moment, timeout_seconds):
            return PublicationVerdict(
                state=ImagePublication.REFUSED,
                detail=(
                    f"no {CI_WORKFLOW} run for {commit_sha} on {DEFAULT_BRANCH} appeared "
                    f"within {timeout_seconds}s, so its images were never published"
                ),
            )
        return PublicationVerdict(
            state=ImagePublication.PENDING,
            detail=f"no {CI_WORKFLOW} run for {commit_sha} on {DEFAULT_BRANCH} yet",
        )

    if run["status"] != _COMPLETED_STATUS:
        if _expired(waiting_since, moment, timeout_seconds):
            return PublicationVerdict(
                state=ImagePublication.REFUSED,
                detail=(
                    f"{CI_WORKFLOW} run {run['id']} for {commit_sha} was still "
                    f"{run['status']} after {timeout_seconds}s"
                ),
                ci_run_id=run["id"],
                ci_conclusion=run.get("conclusion"),
                ci_run_url=run.get("html_url"),
            )
        return PublicationVerdict(
            state=ImagePublication.PENDING,
            detail=f"{CI_WORKFLOW} run {run['id']} for {commit_sha} is {run['status']}",
            ci_run_id=run["id"],
            ci_conclusion=run.get("conclusion"),
            ci_run_url=run.get("html_url"),
        )

    if run.get("conclusion") == _PUBLISHED_CONCLUSION:
        return PublicationVerdict(
            state=ImagePublication.PUBLISHED,
            detail=f"{CI_WORKFLOW} run {run['id']} published the images of {commit_sha}",
            ci_run_id=run["id"],
            ci_conclusion=run.get("conclusion"),
            ci_run_url=run.get("html_url"),
        )

    return PublicationVerdict(
        state=ImagePublication.REFUSED,
        detail=(
            f"{CI_WORKFLOW} run {run['id']} for {commit_sha} ended "
            f"{run.get('conclusion')}, so its images were never published"
        ),
        ci_run_id=run["id"],
        ci_conclusion=run.get("conclusion"),
        ci_run_url=run.get("html_url"),
    )
