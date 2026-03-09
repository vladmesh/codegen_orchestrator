"""Integration tests for LangGraph engineering worker with real services.

Tests exercise the full flow: Redis queue → engineering worker → API/DB.
GitHub/LLM APIs are NOT configured — tests verify behavior up to that boundary.
"""

from uuid import uuid4

import pytest

from shared.contracts.queues.engineering import EngineeringMessage

from .conftest import poll_task_status


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
        project_id = f"int-eng-{suffix}"
        task_id = f"eng-task-{suffix}"
        server_handle = f"int-server-{suffix}"

        # Seed real data via API → DB
        await seed_project(
            project_id,
            name="Integration Test Project",
            status="draft",
            config={"description": "Test project", "modules": ["backend"], "estimated_ram_mb": 512},
        )
        await seed_task(task_id, task_type="engineering", project_id=project_id)
        await seed_server(server_handle, status="ready", capacity_ram_mb=8192)

        # Queue engineering message
        msg = EngineeringMessage(
            task_id=task_id,
            project_id=project_id,
            user_id="test-user-1",
            action="create",
            description="Build a sample microservice",
            skip_deploy=True,
        )
        await redis_client.xadd("engineering:queue", {"data": msg.model_dump_json()})

        # Poll until task reaches terminal state
        task = await poll_task_status(
            api_client, task_id, target_statuses={"running", "failed"}, timeout=30
        )

        # Worker must have picked it up (status moved from "queued")
        assert task["status"] in {"running", "failed"}, f"Unexpected status: {task['status']}"

        # If it reached "running", wait for it to fail at GitHub boundary
        if task["status"] == "running":
            task = await poll_task_status(
                api_client, task_id, target_statuses={"failed"}, timeout=30
            )

        # Should fail because GITHUB_ORG is not set
        assert task["status"] == "failed"
        assert task["error_message"] is not None
        error_lower = task["error_message"].lower()
        assert any(keyword in error_lower for keyword in ["github", "github_org", "not set"]), (
            f"Expected GitHub-related error, got: {task['error_message']}"
        )

    async def test_engineering_worker_missing_project_fails_task(
        self, redis_client, api_client, seed_task
    ):
        """Engineering worker fails task when project doesn't exist in DB."""
        suffix = uuid4().hex[:6]
        task_id = f"eng-missing-{suffix}"
        fake_project_id = f"nonexistent-{suffix}"

        # Seed task without project_id (FK would reject non-existent project_id)
        # The engineering worker reads project_id from the queue message, not the task record
        await seed_task(task_id, task_type="engineering")

        # Queue message referencing non-existent project
        msg = EngineeringMessage(
            task_id=task_id,
            project_id=fake_project_id,
            user_id="test-user-2",
            action="create",
        )
        await redis_client.xadd("engineering:queue", {"data": msg.model_dump_json()})

        # Poll until task fails
        task = await poll_task_status(api_client, task_id, target_statuses={"failed"}, timeout=30)

        assert task["status"] == "failed"
        assert "not found" in task["error_message"].lower()

    async def test_engineering_worker_scaffold_failed_aborts(
        self, redis_client, api_client, seed_project, seed_task
    ):
        """Engineering worker aborts immediately for scaffold_failed projects."""
        suffix = uuid4().hex[:6]
        project_id = f"int-scaffold-fail-{suffix}"
        task_id = f"eng-sf-{suffix}"

        # Seed project with scaffold_failed status
        await seed_project(
            project_id,
            name="Scaffold Failed Project",
            status="scaffold_failed",
            config={"description": "Previously failed scaffold"},
        )
        await seed_task(task_id, task_type="engineering", project_id=project_id)

        # Queue message
        msg = EngineeringMessage(
            task_id=task_id,
            project_id=project_id,
            user_id="test-user-3",
            action="create",
        )
        await redis_client.xadd("engineering:queue", {"data": msg.model_dump_json()})

        # Poll until task fails
        task = await poll_task_status(api_client, task_id, target_statuses={"failed"}, timeout=30)

        assert task["status"] == "failed"
        assert "scaffold_failed" in task["error_message"].lower()
