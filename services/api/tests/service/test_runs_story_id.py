"""Service test: Run.story_id field — create, filter, and query."""

from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest

TASK_TEST_TELEGRAM_ID = 999000999
TASK_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


async def _create_story(client: AsyncClient, title: str) -> str:
    """Create a story and return its id."""
    resp = await client.post(
        "/api/stories/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": title},
        headers={"X-Telegram-ID": str(TASK_TEST_TELEGRAM_ID)},
    )
    assert resp.status_code == HTTPStatus.CREATED
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_create_run_with_story_id(async_client: AsyncClient, _tasks_project):
    """Run can be created with story_id and returned in response."""
    story_id = await _create_story(async_client, "Run story test")

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    resp = await async_client.post(
        "/api/runs/",
        json={
            "id": run_id,
            "type": "deploy",
            "project_id": TASK_TEST_PROJECT_ID,
            "story_id": story_id,
        },
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.json()["story_id"] == story_id


@pytest.mark.asyncio
async def test_filter_runs_by_story_id(async_client: AsyncClient, _tasks_project):
    """GET /runs/?story_id=X returns only runs for that story."""
    story_a = await _create_story(async_client, "Filter test A")
    story_b = await _create_story(async_client, "Filter test B")

    run_a = f"run-a-{uuid.uuid4().hex[:8]}"
    run_b = f"run-b-{uuid.uuid4().hex[:8]}"

    await async_client.post(
        "/api/runs/",
        json={
            "id": run_a,
            "type": "deploy",
            "project_id": TASK_TEST_PROJECT_ID,
            "story_id": story_a,
        },
    )
    await async_client.post(
        "/api/runs/",
        json={
            "id": run_b,
            "type": "deploy",
            "project_id": TASK_TEST_PROJECT_ID,
            "story_id": story_b,
        },
    )

    resp = await async_client.get("/api/runs/", params={"story_id": story_a})
    assert resp.status_code == HTTPStatus.OK
    runs = resp.json()
    assert any(r["id"] == run_a for r in runs)
    assert not any(r["id"] == run_b for r in runs)


@pytest.mark.asyncio
async def test_create_run_without_story_id(async_client: AsyncClient, _tasks_project):
    """Run without story_id has null story_id (standalone deploy)."""
    run_id = f"run-nostory-{uuid.uuid4().hex[:8]}"
    resp = await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "deploy", "project_id": TASK_TEST_PROJECT_ID},
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.json()["story_id"] is None
