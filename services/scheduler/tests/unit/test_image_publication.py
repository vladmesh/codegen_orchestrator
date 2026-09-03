"""The wait that sits ahead of the deploy Run, and what it refuses."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.tasks.image_publication import (
    IMAGE_PUBLICATION_TIMEOUT_SECONDS,
    ImagePublication,
    image_publication_for_commit,
)

BUILT_SHA = "c13d21b9" + "1" * 32
MERGED_AT = datetime(2026, 9, 3, 12, 24, 51, tzinfo=UTC)
JUST_AFTER = MERGED_AT + timedelta(seconds=30)
PAST_THE_BOUND = MERGED_AT + timedelta(seconds=IMAGE_PUBLICATION_TIMEOUT_SECONDS + 1)


def _github(run):
    github = AsyncMock()
    github.get_latest_workflow_run = AsyncMock(return_value=run)
    return github


def _run(status, conclusion):
    return {
        "id": 4242,
        "status": status,
        "conclusion": conclusion,
        "html_url": "https://github.com/o/r/actions/runs/4242",
        "created_at": "2026-09-03T12:24:55Z",
        "head_sha": BUILT_SHA,
    }


@pytest.mark.asyncio
async def test_a_successful_ci_run_for_the_commit_is_publication():
    github = _github(_run("completed", "success"))

    verdict = await image_publication_for_commit(
        github, "o", "r", BUILT_SHA, waiting_since=MERGED_AT, now=JUST_AFTER
    )

    assert verdict.state is ImagePublication.PUBLISHED
    assert verdict.ci_run_id == 4242
    assert github.get_latest_workflow_run.await_args[1]["head_sha"] == BUILT_SHA


@pytest.mark.asyncio
async def test_a_run_still_going_is_pending_inside_the_bound():
    github = _github(_run("in_progress", None))

    verdict = await image_publication_for_commit(
        github, "o", "r", BUILT_SHA, waiting_since=MERGED_AT, now=JUST_AFTER
    )

    assert verdict.state is ImagePublication.PENDING


@pytest.mark.asyncio
async def test_no_run_yet_is_pending_inside_the_bound():
    """The deploy used to dispatch nine seconds after the merge; this is that moment."""
    github = _github(None)

    verdict = await image_publication_for_commit(
        github, "o", "r", BUILT_SHA, waiting_since=MERGED_AT, now=MERGED_AT + timedelta(seconds=9)
    )

    assert verdict.state is ImagePublication.PENDING


@pytest.mark.asyncio
async def test_a_run_still_going_past_the_bound_is_refused():
    github = _github(_run("in_progress", None))

    verdict = await image_publication_for_commit(
        github, "o", "r", BUILT_SHA, waiting_since=MERGED_AT, now=PAST_THE_BOUND
    )

    assert verdict.state is ImagePublication.REFUSED
    assert str(IMAGE_PUBLICATION_TIMEOUT_SECONDS) in verdict.detail


@pytest.mark.asyncio
async def test_a_failed_ci_run_is_refused_at_once_rather_than_waited_out():
    """A finished run that did not publish never will, so the bound is not spent on it."""
    github = _github(_run("completed", "failure"))

    verdict = await image_publication_for_commit(
        github, "o", "r", BUILT_SHA, waiting_since=MERGED_AT, now=JUST_AFTER
    )

    assert verdict.state is ImagePublication.REFUSED
    assert verdict.ci_conclusion == "failure"


@pytest.mark.asyncio
async def test_github_that_cannot_be_read_stays_pending_inside_the_bound():
    """Not asked is not an answer about the project."""
    github = AsyncMock()
    github.get_latest_workflow_run = AsyncMock(side_effect=RuntimeError("502"))

    verdict = await image_publication_for_commit(
        github, "o", "r", BUILT_SHA, waiting_since=MERGED_AT, now=JUST_AFTER
    )

    assert verdict.state is ImagePublication.PENDING


@pytest.mark.asyncio
async def test_a_caller_with_no_start_moment_never_expires():
    github = _github(None)

    verdict = await image_publication_for_commit(
        github, "o", "r", BUILT_SHA, waiting_since=None, now=PAST_THE_BOUND
    )

    assert verdict.state is ImagePublication.PENDING
