"""
E2E Test: Engineering Flow

Tests the complete engineering flow:
1. Create project via API
2. Publish EngineeringMessage to Redis
3. Engineering worker creates repo + sets secrets
4. Developer node spawns worker with ScaffoldConfig
5. Worker-manager runs copier + make setup
6. Developer worker implements features
7. Result published back

This test uses:
- Real GitHub API (test org: project-factory-test)
- Real Claude CLI (requires CLAUDE_SESSION_DIR)
- Real Redis (in Docker)
"""

import asyncio
import os
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from shared.contracts.dto.project import ProjectStatus, ServiceModule
from shared.contracts.queues.engineering import EngineeringMessage

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

# Configuration
API_URL = os.getenv("API_URL", "http://api:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
GITHUB_ORG = os.getenv("GITHUB_ORG", "project-factory-test")


@pytest_asyncio.fixture
async def redis():
    """Redis client for test."""
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def api_client():
    """HTTP client for API."""
    return httpx.AsyncClient(base_url=API_URL, timeout=30.0)


async def wait_for_project_status(
    client: httpx.AsyncClient,
    project_id: str,
    target_status: ProjectStatus,
    timeout: int = 300,
) -> dict:
    """Poll API until project reaches target status."""
    start = time.time()
    while time.time() - start < timeout:
        resp = await client.get(f"/api/projects/{project_id}")
        if resp.status_code == 200:
            project = resp.json()
            current_status = project.get("status")
            if current_status == target_status.value:
                return project
            if current_status in [ProjectStatus.FAILED.value]:
                raise AssertionError(f"Project failed: {project}")
        await asyncio.sleep(5)
    raise TimeoutError(f"Project {project_id} did not reach {target_status} within {timeout}s")


@pytest.fixture
def unique_project_name():
    """Generate unique project name for test."""
    return f"e2e-test-{uuid.uuid4().hex[:8]}"


class TestEngineeringFlow:
    """E2E tests for the complete engineering flow."""

    @pytest.mark.skip(reason="Full flow test - enable when all services ready")
    async def test_engineering_creates_scaffolded_project(
        self, redis: Redis, api_client: httpx.AsyncClient, unique_project_name: str
    ):
        """
        Test that publishing EngineeringMessage results in a scaffolded project.

        Flow:
        1. Create project in API (status: DRAFT)
        2. Create task in API
        3. Publish EngineeringMessage to engineering:queue
        4. Engineering worker creates repo + sets secrets (status: SCAFFOLDING)
        5. Developer node spawns worker with ScaffoldConfig
        6. Worker-manager runs copier + make setup + git push
        7. Developer node updates status to SCAFFOLDED
        8. Developer worker implements business logic
        """

        # Step 1: Create project
        project_resp = await api_client.post(
            "/api/projects",
            json={
                "name": unique_project_name,
                "description": "E2E test project",
                "modules": [ServiceModule.BACKEND.value],
            },
        )
        assert project_resp.status_code == 201, f"Failed to create project: {project_resp.text}"
        project = project_resp.json()
        project_id = project["id"]

        # Step 2: Create task
        task_resp = await api_client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "type": "engineering",
                "spec": "Create a simple hello world API",
            },
        )
        assert task_resp.status_code == 201
        task = task_resp.json()
        task_id = task["id"]

        try:
            # Step 3: Publish EngineeringMessage
            msg = EngineeringMessage(
                task_id=task_id,
                project_id=project_id,
                user_id=1,  # Test user
            )
            await redis.xadd("engineering:queue", {"data": msg.model_dump_json()})

            # Step 4: Wait for SCAFFOLDED status
            scaffolded_project = await wait_for_project_status(
                api_client, project_id, ProjectStatus.SCAFFOLDED, timeout=180
            )

            # Step 5: Verify
            assert scaffolded_project["status"] == ProjectStatus.SCAFFOLDED.value
            assert scaffolded_project["repository_url"] is not None
            assert GITHUB_ORG in scaffolded_project["repository_url"]

        finally:
            # Cleanup: Delete project (will cascade to task)
            await api_client.delete(f"/api/projects/{project_id}")
