"""Service tests: a Story says what it is waiting for, and only a transition says it.

`waiting_on` is written by the same server action that performs the transition —
`_do_transition` for a single hop, `_apply_chain` for a composite — from the one
`WAITING_ON_BY_STATUS` mapping, in the same transaction and on the same locked
row as `status`.  These tests hold that from outside the API: what a transition
committed, what `PATCH /stories/{id}` is refused, and what the administrator
overview reports.
"""

from fastapi import status
from httpx import AsyncClient
import pytest

from shared.contracts.dto.story import StoryStatus, StoryWaitingOn

TASK_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


async def _create_story(async_client: AsyncClient, title: str) -> str:
    created = await async_client.post(
        "/api/stories/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": title},
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    body = created.json()
    # A fresh story waits on nothing, and the read schema says so rather than
    # leaving the caller to infer it from a null.
    assert body["waiting_on"] == StoryWaitingOn.NONE
    return body["id"]


@pytest.mark.asyncio
async def test_single_hop_transitions_commit_the_wait_their_status_implies(
    async_client: AsyncClient, _tasks_project
) -> None:
    """Each hop writes `waiting_on` with `status`, and the committed row carries both."""
    story_id = await _create_story(async_client, "Single-hop waiting_on")

    # created → in_progress → pr_review → deploying → testing.  Work in flight
    # waits on nothing; a PR waits on CI, a deploy on the deploy, QA on QA.
    hops = [
        ("start", StoryStatus.IN_PROGRESS, StoryWaitingOn.NONE),
        ("pr_review", StoryStatus.PR_REVIEW, StoryWaitingOn.CI),
        ("deploy", StoryStatus.DEPLOYING, StoryWaitingOn.DEPLOY),
        ("test", StoryStatus.TESTING, StoryWaitingOn.QA),
    ]
    for action, expected_status, expected_wait in hops:
        resp = await async_client.post(f"/api/stories/{story_id}/{action}", json={"actor": "po"})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["status"] == expected_status
        assert resp.json()["waiting_on"] == expected_wait

        # Re-read the row: the wait was committed with the status, not attached
        # to the response by the endpoint that answered.
        committed = await async_client.get(f"/api/stories/{story_id}")
        assert committed.json()["status"] == expected_status
        assert committed.json()["waiting_on"] == expected_wait


@pytest.mark.asyncio
async def test_a_wait_is_cleared_by_the_transition_that_ends_it(
    async_client: AsyncClient, _tasks_project
) -> None:
    """Leaving a waiting status clears the wait — nothing else has to notice."""
    story_id = await _create_story(async_client, "Cleared waiting_on")

    await async_client.post(f"/api/stories/{story_id}/start", json={"actor": "po"})
    parked = await async_client.post(f"/api/stories/{story_id}/human-review", json={"actor": "po"})
    assert parked.status_code == status.HTTP_200_OK, parked.text
    assert parked.json()["waiting_on"] == StoryWaitingOn.HUMAN_REVIEW

    resumed = await async_client.post(f"/api/stories/{story_id}/start", json={"actor": "po"})
    assert resumed.status_code == status.HTTP_200_OK, resumed.text
    assert resumed.json()["status"] == StoryStatus.IN_PROGRESS
    assert resumed.json()["waiting_on"] == StoryWaitingOn.NONE


@pytest.mark.asyncio
async def test_composite_ci_retry_commits_the_wait_of_the_status_it_lands_on(
    async_client: AsyncClient, _tasks_project
) -> None:
    """The composite lands on in_progress, so it commits `none` — not `ci`, not a stale wait."""
    story_id = await _create_story(async_client, "Composite waiting_on")
    await async_client.post(f"/api/stories/{story_id}/start", json={"actor": "po"})
    review = await async_client.post(f"/api/stories/{story_id}/pr_review", json={"actor": "po"})
    assert review.json()["waiting_on"] == StoryWaitingOn.CI

    resp = await async_client.post(
        f"/api/stories/{story_id}/retry-after-ci-failure", json={"actor": "scheduler"}
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == StoryStatus.IN_PROGRESS
    assert resp.json()["waiting_on"] == StoryWaitingOn.NONE

    committed = await async_client.get(f"/api/stories/{story_id}")
    assert committed.json()["status"] == StoryStatus.IN_PROGRESS
    assert committed.json()["waiting_on"] == StoryWaitingOn.NONE


@pytest.mark.asyncio
async def test_patch_refuses_to_write_waiting_on(async_client: AsyncClient, _tasks_project) -> None:
    """A poller cannot set the field: PATCH is the poller-shaped path, and it is refused."""
    story_id = await _create_story(async_client, "Patched waiting_on")
    await async_client.post(f"/api/stories/{story_id}/start", json={"actor": "po"})
    await async_client.post(f"/api/stories/{story_id}/pr_review", json={"actor": "po"})

    resp = await async_client.patch(
        f"/api/stories/{story_id}",
        json={"title": "Renamed too", "waiting_on": StoryWaitingOn.RESOURCES.value},
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    # The refusal is the whole request, so the legal field in the same body did
    # not land either: a caller gets one answer about one write.
    committed = await async_client.get(f"/api/stories/{story_id}")
    assert committed.json()["title"] == "Patched waiting_on"
    assert committed.json()["waiting_on"] == StoryWaitingOn.CI


@pytest.mark.asyncio
async def test_admin_overview_reports_the_wait_each_story_committed(
    async_client: AsyncClient, _tasks_project
) -> None:
    """The bounded per-story section carries id, project, status and the wait."""
    story_id = await _create_story(async_client, "Overview waiting_on")
    await async_client.post(f"/api/stories/{story_id}/start", json={"actor": "po"})
    await async_client.post(f"/api/stories/{story_id}/pr_review", json={"actor": "po"})
    await async_client.post(f"/api/stories/{story_id}/deploy", json={"actor": "po"})

    overview = await async_client.get("/api/admin/overview")
    assert overview.status_code == status.HTTP_200_OK, overview.text
    waiting = overview.json()["waiting_stories"]

    from src.routers.admin_overview import WAITING_STORY_LIMIT

    assert len(waiting) <= WAITING_STORY_LIMIT
    # Ordered most-recently-touched first, so the story just moved is present.
    mine = [entry for entry in waiting if entry["story_id"] == story_id]
    assert mine, waiting
    assert mine[0]["project_id"] == TASK_TEST_PROJECT_ID
    assert mine[0]["status"] == StoryStatus.DEPLOYING
    assert mine[0]["waiting_on"] == StoryWaitingOn.DEPLOY
    # A story that waits on nothing is not in the section at all.
    assert all(entry["waiting_on"] != StoryWaitingOn.NONE for entry in waiting)
