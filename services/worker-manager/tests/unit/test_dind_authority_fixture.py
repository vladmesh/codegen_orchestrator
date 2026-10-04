"""Offline checks of the test producer; real authority is exercised only in CI."""

from uuid import uuid4

import httpx
import pytest

from tests.integration.backend.worker_authority import seed_worker_authority


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [None, "project", "story", "task", "run", "disposition"])
async def test_fixture_requires_matching_persisted_readback(corrupt):
    project_id = str(uuid4())
    project = {"id": project_id, "initiating_run_id": "fixture-run"}
    story = {"id": "story-fixture", "project_id": project_id}
    task = {"id": "task-fixture", "project_id": project_id, "story_id": story["id"]}
    run = {
        "id": "eng-fixture",
        "type": "engineering",
        "status": "queued",
        "project_id": project_id,
        "story_id": story["id"],
        "task_id": task["id"],
    }
    reads = []

    async def seed_project(**kwargs):
        project["initiating_run_id"] = kwargs["initiating_run_id"]
        return project.copy()

    async def seed_task(**kwargs):
        assert kwargs["story_id"] == story["id"]
        return task.copy()

    def handle(request):
        path = request.url.path
        reads.append((request.method, path))
        if path == "/api/stories/":
            return httpx.Response(201, json=story)
        if path.endswith("/start"):
            return httpx.Response(200, json=story)
        if path == "/api/work-admission/engineering-dispatches":
            return httpx.Response(
                200,
                json={
                    "outcome": "admitted",
                    "run_id": run["id"],
                    "initiating_run_id": project["initiating_run_id"],
                },
            )
        if path.endswith("engineering-disposition"):
            return httpx.Response(
                200,
                json={
                    "disposition": "stopped" if corrupt == "disposition" else "eligible",
                    "project_id": project_id,
                    "story_id": story["id"],
                    "attempt_id": run["id"],
                    "initiating_run_id": project["initiating_run_id"],
                },
            )
        kind, row = {
            f"/api/projects/{project_id}": ("project", project),
            f"/api/stories/{story['id']}": ("story", story),
            f"/api/tasks/{task['id']}": ("task", task),
            f"/api/runs/{run['id']}": ("run", run),
        }[path]
        row = row.copy()
        if corrupt == kind:
            row["initiating_run_id" if kind == "project" else "project_id"] = "foreign"
        return httpx.Response(200, json=row)

    owners = []
    async with httpx.AsyncClient(
        base_url="http://fixture", transport=httpx.MockTransport(handle)
    ) as api:
        if corrupt:
            with pytest.raises(AssertionError):
                await seed_worker_authority(api, seed_project, seed_task, owners)
        else:
            owner = await seed_worker_authority(api, seed_project, seed_task, owners)
            assert owner.project_id == project_id
            assert owner.story_id == story["id"]
            assert owner.attempt_id == run["id"]
            assert owner.run_id == project["initiating_run_id"]
            assert len([method for method, _ in reads if method == "GET"]) == 4
    assert len(owners) == 1  # Failed readback still leaves an owned cleanup target.
    assert not any("publish" in path or "spawn-worker" in path for _, path in reads)
