"""Service tests: a composite Story move applies all of its hops, or none.

The CI-failure retry used to be three client calls (`fail`, `reopen`, `start`).
It is one endpoint now, so the whole chain has to behave like one write: every
hop validated against ``VALID_TRANSITIONS`` before any hop is applied, and
``reopened_at`` stamped exactly as the single `reopen` hop stamps it.

A Story keeps no status-change event table — the row itself is the record — so
"no partial status change was written" is asserted on the two fields the chain
writes: ``status`` and ``reopened_at``.
"""

from fastapi import status
from httpx import AsyncClient
import pytest

from shared.contracts.dto.story import StoryStatus

TASK_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


async def _story_in_pr_review(async_client: AsyncClient) -> str:
    """A story parked where a CI failure finds it: created → in_progress → pr_review."""
    created = await async_client.post(
        "/api/stories/",
        json={
            "project_id": TASK_TEST_PROJECT_ID,
            "title": "Composite CI retry",
            "description": "CI failed on the story branch",
        },
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    story_id = created.json()["id"]

    started = await async_client.post(f"/api/stories/{story_id}/start", json={"actor": "po"})
    assert started.status_code == status.HTTP_200_OK, started.text
    review = await async_client.post(f"/api/stories/{story_id}/pr_review", json={"actor": "po"})
    assert review.status_code == status.HTTP_200_OK, review.text
    assert review.json()["reopened_at"] is None

    return story_id


@pytest.mark.asyncio
async def test_ci_retry_applies_every_hop_in_one_call(
    async_client: AsyncClient, _tasks_project
) -> None:
    """failed → reopened → in_progress lands in one request, with reopened_at stamped."""
    story_id = await _story_in_pr_review(async_client)

    resp = await async_client.post(
        f"/api/stories/{story_id}/retry-after-ci-failure", json={"actor": "scheduler"}
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["status"] == StoryStatus.IN_PROGRESS
    # The reopen hop starts the current work cycle; completion reads this stamp
    # to refuse pre-reopen QA evidence, so the composite must write it.
    assert body["reopened_at"] is not None

    final = await async_client.get(f"/api/stories/{story_id}")
    assert final.json()["status"] == StoryStatus.IN_PROGRESS
    assert final.json()["reopened_at"] == body["reopened_at"]


@pytest.mark.asyncio
async def test_ci_retry_applies_nothing_when_a_later_hop_is_invalid(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, _tasks_project
) -> None:
    """An illegal hop anywhere in the chain leaves the story exactly as it was."""
    from src.routers import _story_actions

    story_id = await _story_in_pr_review(async_client)

    # failed → deploying is not in VALID_TRANSITIONS. The first hop of the chain
    # is legal, so a validate-as-you-go implementation would apply it and stop
    # halfway; nothing on this path may run.
    monkeypatch.setitem(
        _story_actions.COMPOSITE_CHAINS,
        _story_actions.RETRY_AFTER_CI_FAILURE,
        (StoryStatus.FAILED, StoryStatus.DEPLOYING, StoryStatus.IN_PROGRESS),
    )

    resp = await async_client.post(
        f"/api/stories/{story_id}/retry-after-ci-failure", json={"actor": "scheduler"}
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text

    final = await async_client.get(f"/api/stories/{story_id}")
    assert final.status_code == status.HTTP_200_OK, final.text
    # Neither the legal first hop nor the reopen stamp reached the row.
    assert final.json()["status"] == StoryStatus.PR_REVIEW
    assert final.json()["reopened_at"] is None


@pytest.mark.asyncio
async def test_ci_retry_is_refused_when_the_first_hop_is_illegal(
    async_client: AsyncClient, _tasks_project
) -> None:
    """A story already in `failed` is refused outright — no hop of the chain runs."""
    story_id = await _story_in_pr_review(async_client)
    failed = await async_client.post(f"/api/stories/{story_id}/fail", json={"actor": "po"})
    assert failed.status_code == status.HTTP_200_OK, failed.text

    # failed → failed is not a legal hop, so the whole move is refused.
    resp = await async_client.post(
        f"/api/stories/{story_id}/retry-after-ci-failure", json={"actor": "scheduler"}
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    final = await async_client.get(f"/api/stories/{story_id}")
    assert final.json()["status"] == StoryStatus.FAILED
    assert final.json()["reopened_at"] is None
