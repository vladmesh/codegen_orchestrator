"""A Story's committed entry into PR review publishes the PR/CI sweep's hint."""

from _api_calls import call_api
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.models import Story

TASK_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"
PR_NUMBER = 42


async def _committed_review_state(db_engine, story_id: str) -> tuple[str, str, int | None]:
    async with AsyncSession(db_engine) as session:
        row = (
            await session.execute(
                select(Story.status, Story.waiting_on, Story.pr_number).where(Story.id == story_id)
            )
        ).one()
        return row.status, row.waiting_on, row.pr_number


@pytest.mark.asyncio
async def test_a_committed_pr_review_entry_publishes_one_hint_and_a_repeat_none(
    async_client, _tasks_project, wake_hints, db_engine
):
    story = await call_api(
        async_client,
        "POST",
        "/api/stories/",
        201,
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "PR review wake"},
    )
    story_id = story["id"]
    await call_api(async_client, "POST", f"/api/stories/{story_id}/start", json={"actor": "test"})
    await call_api(async_client, "PATCH", f"/api/stories/{story_id}", json={"pr_number": PR_NUMBER})
    assert await wake_hints.published() == []

    review = await call_api(
        async_client, "POST", f"/api/stories/{story_id}/pr_review", json={"actor": "scheduler"}
    )

    assert review["status"] == "pr_review"
    assert await wake_hints.published() == [SchedulerWakeTarget.PR_CI]
    assert await _committed_review_state(db_engine, story_id) == ("pr_review", "ci", PR_NUMBER)

    await call_api(
        async_client,
        "POST",
        f"/api/stories/{story_id}/pr_review",
        422,
        json={"actor": "scheduler"},
    )

    assert await wake_hints.published() == []
    assert await _committed_review_state(db_engine, story_id) == ("pr_review", "ci", PR_NUMBER)
