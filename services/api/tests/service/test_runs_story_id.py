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
async def test_list_runs_hides_unowned_runs_from_a_non_admin_caller(
    async_client: AsyncClient, _tasks_project
):
    """A non-admin X-Telegram-ID narrows the result even with a valid internal key.

    pr_poller creates deploy runs with no user_id, so this rule answers `[]` for
    them to any user-scoped caller. The live mega harness relies on it: it must
    observe deploy runs as a plain internal service, never as its own non-admin
    user, or it waits out a deploy that already succeeded (2026-07-16).
    """
    telegram_id = 999000998
    await async_client.post(
        "/api/users/upsert",
        json={"telegram_id": telegram_id, "username": "non_admin", "first_name": "Non"},
    )
    story_id = await _create_story(async_client, "Ownership narrowing test")

    run_id = f"deploy-poll-{uuid.uuid4().hex[:8]}"
    resp = await async_client.post(
        "/api/runs/",
        json={
            "id": run_id,
            "type": "deploy",
            "project_id": TASK_TEST_PROJECT_ID,
            "story_id": story_id,
            "run_metadata": {"triggered_by": "pr_poll", "head_sha": "abc123"},
        },
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.json()["user_id"] is None

    as_user = await async_client.get(
        "/api/runs/",
        params={"story_id": story_id, "run_type": "deploy"},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert as_user.status_code == HTTPStatus.OK
    assert as_user.json() == []

    as_service = await async_client.get(
        "/api/runs/",
        params={"story_id": story_id, "run_type": "deploy"},
    )
    assert as_service.status_code == HTTPStatus.OK
    assert [run["id"] for run in as_service.json()] == [run_id]


@pytest.mark.asyncio
async def test_create_run_with_task_id_is_filterable(async_client: AsyncClient, _tasks_project):
    """A run created with task_id is found by GET /runs/?task_id=...&status=...

    The dispatcher's pre-dispatch guard asks exactly this question to decide
    whether a task already has a live engineering run.
    """
    resp = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": TASK_TEST_PROJECT_ID,
            "title": "Run task link test",
            "type": "feature",
        },
    )
    assert resp.status_code == HTTPStatus.CREATED
    task_id = resp.json()["id"]

    run_id = f"eng-{uuid.uuid4().hex[:12]}"
    resp = await async_client.post(
        "/api/runs/",
        json={
            "id": run_id,
            "type": "engineering",
            "project_id": TASK_TEST_PROJECT_ID,
            "task_id": task_id,
        },
    )
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.json()["task_id"] == task_id

    resp = await async_client.get(
        "/api/runs/",
        params={"task_id": task_id, "run_type": "engineering", "status": "queued"},
    )
    assert resp.status_code == HTTPStatus.OK
    assert [r["id"] for r in resp.json()] == [run_id]

    # Once terminal, the run no longer answers the "is there a live run" question
    resp = await async_client.patch(f"/api/runs/{run_id}", json={"status": "failed"})
    assert resp.status_code == HTTPStatus.OK
    resp = await async_client.get(
        "/api/runs/",
        params={"task_id": task_id, "run_type": "engineering", "status": "queued"},
    )
    assert resp.json() == []


@pytest.mark.asyncio
async def test_patch_run_metadata_is_persisted(async_client: AsyncClient, _tasks_project):
    """PATCH merges run_metadata and the merged value survives the commit.

    The dispatcher's publish-failure compensation clears `iteration` on the run it
    just created, so the next tick dispatches a fresh one instead of recovering a
    run that never reached the queue. run_metadata is a plain JSON column, so an
    in-place mutation would not be written back.
    """
    run_id = f"eng-{uuid.uuid4().hex[:12]}"
    resp = await async_client.post(
        "/api/runs/",
        json={
            "id": run_id,
            "type": "engineering",
            "project_id": TASK_TEST_PROJECT_ID,
            "run_metadata": {"triggered_by": "dispatcher", "iteration": 1},
        },
    )
    assert resp.status_code == HTTPStatus.CREATED

    resp = await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "failed",
            "run_metadata": {"iteration": None, "publish_failed": True},
        },
    )
    assert resp.status_code == HTTPStatus.OK

    resp = await async_client.get(f"/api/runs/{run_id}")
    assert resp.status_code == HTTPStatus.OK
    metadata = resp.json()["run_metadata"]
    assert metadata["iteration"] is None
    assert metadata["publish_failed"] is True
    assert metadata["triggered_by"] == "dispatcher"


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
