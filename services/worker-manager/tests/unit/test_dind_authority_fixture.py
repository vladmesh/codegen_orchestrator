"""Offline checks of the test producer; real authority is exercised only in CI."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml

from shared.contracts.queues.worker import WorkerOwnership
from tests.integration.backend.worker_authority import (
    assert_persisted_worker_authority,
    seed_worker_authority,
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("story_id", [None, "story-fixture"])
@pytest.mark.parametrize(
    "corrupt",
    [
        None,
        "task_story",
        "run_story",
        "task_project",
        "run_project",
        "run_task",
        "run_status",
        "run_type",
        "initiating",
        "disposition",
    ],
)
async def test_readback_proves_story_scoped_and_standalone_ownership(story_id, corrupt):
    owner = WorkerOwnership(
        project_id="project", run_id="initiating", attempt_id="attempt", story_id=story_id
    )
    rows = {
        "/api/projects/project": {"id": "project", "initiating_run_id": owner.run_id},
        "/api/tasks/task": {"id": "task", "project_id": "project", "story_id": story_id},
        "/api/runs/attempt": {
            "id": "attempt",
            "project_id": "project",
            "story_id": story_id,
            "task_id": "task",
            "type": "engineering",
            "status": "queued",
        },
        "/api/runs/attempt/engineering-disposition": {
            "project_id": "project",
            "story_id": story_id,
            "attempt_id": "attempt",
            "initiating_run_id": owner.run_id,
            "disposition": "eligible",
        },
    }
    if story_id is not None:
        rows[f"/api/stories/{story_id}"] = {"id": story_id, "project_id": "project"}
    mutations = {
        "task_story": ("/api/tasks/task", "story_id", "foreign-story"),
        "run_story": ("/api/runs/attempt", "story_id", "foreign-story"),
        "task_project": ("/api/tasks/task", "project_id", "foreign-project"),
        "run_project": ("/api/runs/attempt", "project_id", "foreign-project"),
        "run_task": ("/api/runs/attempt", "task_id", "foreign-task"),
        "run_status": ("/api/runs/attempt", "status", "failed"),
        "run_type": ("/api/runs/attempt", "type", "qa"),
        "initiating": ("/api/projects/project", "initiating_run_id", "stale"),
        "disposition": ("/api/runs/attempt/engineering-disposition", "disposition", "stopped"),
    }
    if corrupt is not None:
        path, field, value = mutations[corrupt]
        rows[path][field] = value
    requested = []

    def handle(request):
        requested.append(request.url.path)
        return httpx.Response(200, json=rows[request.url.path])

    async with httpx.AsyncClient(
        base_url="http://fixture", transport=httpx.MockTransport(handle)
    ) as api:
        if corrupt is None:
            await assert_persisted_worker_authority(api, owner, "task")
            assert set(requested) == set(rows)
        else:
            with pytest.raises(AssertionError):
                await assert_persisted_worker_authority(api, owner, "task")
    assert "/api/stories/None" not in requested


def test_dind_attempt_has_no_competing_consumer_and_backend_keeps_real_integration():
    root = Path(__file__).resolve().parents[4]
    stacks = root / "tests/compose/integration"
    dind = yaml.safe_load((stacks / "backend-dind.yml").read_text())["services"]
    backend = yaml.safe_load((stacks / "backend.yml").read_text())["services"]
    # A consumer could settle the deliberately unowned repository before manager
    # creation; eliminating its launch closes that race for every fixture read.
    assert all(
        "src.consumers.engineering" not in service.get("command", []) for service in dind.values()
    )
    assert set(dind["integration-test-runner"]["depends_on"]) <= set(dind)
    assert "worker-manager" in dind["integration-test-runner"]["depends_on"]
    assert "test_run_ownership_propagation.py" in " ".join(
        dind["integration-test-runner"]["command"]
    )
    assert backend["engineering-worker"]["command"] == ["python", "-m", "src.consumers.engineering"]
    assert "engineering-worker" in backend["integration-test-runner"]["depends_on"]
    assert "test_langgraph_integration.py" in " ".join(
        backend["integration-test-runner"]["command"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [None, "terminal", "start", "worker", "readback", "attempt"])
async def test_fixture_turn_prepares_owned_run_before_native_publication(corrupt):
    from tests.integration.backend.worker_authority import publish_worker_fixture_turn

    owner = WorkerOwnership(
        project_id="project", story_id="story", run_id="initiating", attempt_id="attempt"
    )
    run = {
        "id": "foreign" if corrupt == "attempt" else "attempt",
        "project_id": "project",
        "story_id": "story",
        "task_id": "task",
        "type": "engineering",
        "status": "failed" if corrupt == "terminal" else "queued",
        "run_metadata": {"worker_id": "foreign"} if corrupt == "worker" else {},
    }
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/start"):
            started = corrupt != "start"
            run["status"] = "running" if started else "cancelled"
            return httpx.Response(
                200, json={"run_id": "attempt", "started": started, "run_status": run["status"]}
            )
        if request.method == "PATCH":
            body = json.loads(request.content)
            assert body == {
                "run_metadata": {"worker_id": "worker", "initiating_run_id": "initiating"}
            }
            run["run_metadata"] = body["run_metadata"]
            if corrupt == "readback":
                run["run_metadata"]["worker_id"] = "foreign"
            return httpx.Response(200, json=run)
        if path.endswith("/publish-worker-turn"):
            assert json.loads(request.content) == {
                "worker_id": "worker",
                "turn": {
                    "request_id": "request",
                    "attempt_id": "attempt",
                    "turn_deadline_seconds": 60,
                    "prompt": "No model",
                },
            }
            return httpx.Response(200, json={"stream_id": "123-0"})
        rows = {
            "/api/runs/attempt": run,
            "/api/projects/project": {"id": "project", "initiating_run_id": "initiating"},
            "/api/stories/story": {"id": "story", "project_id": "project"},
            "/api/tasks/task": {"id": "task", "project_id": "project", "story_id": "story"},
            "/api/runs/attempt/engineering-disposition": {
                "disposition": "eligible",
                "project_id": "project",
                "story_id": "story",
                "attempt_id": "attempt",
                "initiating_run_id": "initiating",
            },
        }
        return httpx.Response(200, json=rows[path])

    async with httpx.AsyncClient(
        base_url="http://fixture", transport=httpx.MockTransport(handle)
    ) as api:
        if corrupt:
            with pytest.raises(AssertionError):
                await publish_worker_fixture_turn(
                    api,
                    owner,
                    "worker",
                    request_id="request",
                    prompt="No model",
                    turn_deadline_seconds=60,
                )
        else:
            turn, stream_id = await publish_worker_fixture_turn(
                api,
                owner,
                "worker",
                request_id="request",
                prompt="No model",
                turn_deadline_seconds=60,
            )
            assert stream_id == "123-0" and turn.request_id == "request"
            assert calls[0] == ("GET", "/api/runs/attempt")
            assert calls[1:3] == [
                ("POST", "/api/runs/attempt/start"),
                ("PATCH", "/api/runs/attempt"),
            ]
            assert calls[-1] == ("POST", "/api/runs/attempt/publish-worker-turn")
    if corrupt:
        assert not any(path.endswith("publish-worker-turn") for _, path in calls)
    assert not any("work-admission" in path or "engineering:queue" in path for _, path in calls)
