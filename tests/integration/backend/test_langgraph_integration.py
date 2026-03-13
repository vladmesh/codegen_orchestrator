"""Integration tests for LangGraph engineering worker with real services.

Tests exercise the full flow: Redis queue → engineering worker → API/DB.
GitHub/LLM APIs are NOT configured — tests verify behavior up to that boundary.

The engineering worker expects a Run record to exist before processing.
The task_dispatcher creates runs with `id = "eng-xxx"` and puts that as `task_id`
in the EngineeringMessage. We replicate that flow here.
"""

import asyncio
import time
from uuid import uuid4

import pytest

from shared.contracts.queues.engineering import EngineeringMessage


async def _create_run(
    api_client, run_id: str, project_id: str, planning_task_id: str | None = None
):
    """Create a Run record via API (replicates what task_dispatcher does)."""
    body = {
        "id": run_id,
        "type": "engineering",
        "project_id": project_id,
        "run_metadata": {
            "triggered_by": "integration_test",
            "task_id": planning_task_id,
        },
    }
    resp = await api_client.post("/api/runs/", json=body)
    assert resp.status_code == 201, f"Failed to create run: {resp.text}"
    return resp.json()


async def _get_run(api_client, run_id: str) -> dict:
    """Get a Run record by ID."""
    resp = await api_client.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200, f"Run not found: {resp.text}"
    return resp.json()


@pytest.mark.integration
@pytest.mark.asyncio
class TestLangGraphIntegration:
    async def test_engineering_worker_processes_queue_and_updates_task(
        self, redis_client, api_client, seed_project, seed_task, seed_server
    ):
        """Engineering worker picks up queue message, fetches project, updates task status.

        The flow fails at GitHub boundary (_create_repo_and_set_secrets) because
        GITHUB_ORG is not set in the test environment. We verify everything
        before that point works with real DB/Redis/API.
        """
        suffix = uuid4().hex[:6]
        run_id = f"eng-{uuid4().hex[:12]}"
        server_handle = f"int-server-{suffix}"

        # Seed real data via API → DB
        project = await seed_project(
            name="Integration Test Project",
            status="draft",
            config={
                "description": "Test project",
                "modules": ["backend"],
                "estimated_ram_mb": 512,
            },
        )
        project_id = project["id"]
        task = await seed_task(
            title=f"Engineering task {suffix}",
            project_id=project_id,
        )
        await seed_server(server_handle, status="ready", capacity_ram_mb=8192)

        # Create run record (replicates task_dispatcher behavior)
        await _create_run(api_client, run_id, project_id, planning_task_id=task["id"])

        # Queue engineering message (task_id = run_id, not planning task id)
        msg = EngineeringMessage(
            task_id=run_id,
            project_id=project_id,
            user_id="test-user-1",
            action="create",
            description="Build a sample microservice",
            skip_deploy=True,
            planning_task_id=task["id"],
        )
        await redis_client.xadd("engineering:queue", {"data": msg.model_dump_json()})

        # Poll the Run record until it reaches a terminal state
        timeout = 30
        start = time.time()
        run = None
        while time.time() - start < timeout:
            run = await _get_run(api_client, run_id)
            if run["status"] in {"running", "failed", "completed"}:
                break
            await asyncio.sleep(1)

        assert run is not None
        assert run["status"] in {"running", "failed"}, f"Unexpected status: {run['status']}"

        # If it reached "running", wait for it to fail at GitHub boundary
        if run["status"] == "running":
            start = time.time()
            while time.time() - start < timeout:
                run = await _get_run(api_client, run_id)
                if run["status"] == "failed":
                    break
                await asyncio.sleep(1)

        # Should fail because GITHUB_ORG is not set
        assert run["status"] == "failed"
        assert run["error_message"] is not None
        error_lower = run["error_message"].lower()
        assert any(keyword in error_lower for keyword in ["github", "github_org", "not set"]), (
            f"Expected GitHub-related error, got: {run['error_message']}"
        )

    async def test_engineering_worker_missing_project_fails_task(
        self, redis_client, api_client, seed_project, seed_task
    ):
        """Engineering worker fails task when project doesn't exist in DB."""
        suffix = uuid4().hex[:6]
        run_id = f"eng-{uuid4().hex[:12]}"
        fake_project_id = str(uuid4())

        # Seed a project to own the task (FK constraint)
        project = await seed_project(name=f"Owner project {suffix}")
        task = await seed_task(
            title=f"Missing project task {suffix}",
            project_id=project["id"],
        )

        # Create run (referencing fake project)
        await _create_run(api_client, run_id, fake_project_id, planning_task_id=task["id"])

        # Queue message referencing non-existent project
        msg = EngineeringMessage(
            task_id=run_id,
            project_id=fake_project_id,
            user_id="test-user-2",
            action="create",
            planning_task_id=task["id"],
        )
        await redis_client.xadd("engineering:queue", {"data": msg.model_dump_json()})

        # Poll the Run until it fails
        timeout = 30
        start = time.time()
        run = None
        while time.time() - start < timeout:
            run = await _get_run(api_client, run_id)
            if run["status"] == "failed":
                break
            await asyncio.sleep(1)

        assert run is not None
        assert run["status"] == "failed"
        assert run["error_message"] is not None
        assert "not found" in run["error_message"].lower()

    async def test_engineering_worker_non_draft_project_fails_at_boundary(
        self, redis_client, api_client, seed_project, seed_task
    ):
        """Engineering worker fails at resource/GitHub boundary for non-draft projects.

        scaffold_failed projects are not explicitly rejected — the worker proceeds
        but fails at a later boundary (resource allocation, GitHub, etc.).
        """
        suffix = uuid4().hex[:6]
        run_id = f"eng-{uuid4().hex[:12]}"

        # Seed project with scaffold_failed status
        project = await seed_project(
            name="Scaffold Failed Project",
            status="scaffold_failed",
            config={"description": "Previously failed scaffold"},
        )
        project_id = project["id"]
        task = await seed_task(
            title=f"Scaffold failed task {suffix}",
            project_id=project_id,
        )

        # Create run
        await _create_run(api_client, run_id, project_id, planning_task_id=task["id"])

        # Queue message
        msg = EngineeringMessage(
            task_id=run_id,
            project_id=project_id,
            user_id="test-user-3",
            action="create",
            planning_task_id=task["id"],
        )
        await redis_client.xadd("engineering:queue", {"data": msg.model_dump_json()})

        # Poll the Run until it fails (at whatever boundary)
        timeout = 30
        start = time.time()
        run = None
        while time.time() - start < timeout:
            run = await _get_run(api_client, run_id)
            if run["status"] in {"failed", "completed"}:
                break
            await asyncio.sleep(1)

        assert run is not None
        assert run["status"] == "failed"
        assert run["error_message"] is not None
