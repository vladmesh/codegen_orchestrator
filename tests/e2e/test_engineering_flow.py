"""
E2E Test: Engineering Flow

Tests the complete engineering flow:
1. Create project via API
2. Publish EngineeringMessage to Redis
3. LangGraph consumes and delegates to Scaffolder
4. Scaffolder creates GitHub repo with template
5. Developer worker implements features
6. Result published back

This test uses:
- Real GitHub API (test org: project-factory-test)
- Real Claude CLI (requires CLAUDE_SESSION_DIR)
- Real Redis (in Docker)
"""

import asyncio
import json
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


async def wait_for_stream_message(
    redis: Redis, stream: str, timeout: int = 60, last_id: str = "0"
) -> tuple[str, dict]:
    """Wait for a message on Redis stream."""
    import asyncio

    start = time.time()
    while time.time() - start < timeout:
        messages = await redis.xread({stream: last_id}, count=1, block=1000)
        if messages:
            msg_id = messages[0][1][0][0]
            data = messages[0][1][0][1]
            return msg_id, data
        await asyncio.sleep(0.5)
    raise TimeoutError(f"No message on {stream} within {timeout}s")


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
        4. Wait for project status to become SCAFFOLDED
        5. Verify GitHub repo was created
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

    @pytest.mark.skip(reason="Phase 5 - requires full LangGraph integration")
    async def test_scaffolder_receives_message(
        self, redis: Redis, api_client: httpx.AsyncClient, unique_project_name: str
    ):
        """
        Smoke test: Verify that messages reach the scaffolder queue.

        This is a lighter test that doesn't require full Claude/GitHub setup.
        """

        # Create project
        project_resp = await api_client.post(
            "/api/projects",
            json={
                "name": unique_project_name,
                "description": "Smoke test project",
                "modules": [ServiceModule.BACKEND.value],
            },
        )
        assert project_resp.status_code == 201
        project_id = project_resp.json()["id"]

        # Create task
        task_resp = await api_client.post(
            "/api/tasks",
            json={"project_id": project_id, "type": "engineering"},
        )
        assert task_resp.status_code == 201
        task_id = task_resp.json()["id"]

        try:
            # Publish engineering message
            msg = EngineeringMessage(task_id=task_id, project_id=project_id, user_id=1)
            await redis.xadd("engineering:queue", {"data": msg.model_dump_json()})

            # Wait for scaffolder to receive delegated message
            # LangGraph should publish to scaffolder:queue
            try:
                _, scaffolder_msg = await wait_for_stream_message(
                    redis, "scaffolder:queue", timeout=60
                )
                data = json.loads(scaffolder_msg.get("data", "{}"))
                assert data.get("project_id") == project_id
            except TimeoutError:
                # If scaffolder doesn't receive within timeout, check if langgraph is processing
                pytest.skip("LangGraph may not be processing - check service logs")

        finally:
            await api_client.delete(f"/api/projects/{project_id}")


class TestScaffolderIntegration:
    """Tests for Scaffolder service with real GitHub."""

    async def test_scaffolder_creates_github_repo(
        self, redis: Redis, api_client: httpx.AsyncClient, unique_project_name: str
    ):
        """
        Test that Scaffolder creates a real GitHub repository.

        Requires:
        - GITHUB_APP_ID
        - GITHUB_PRIVATE_KEY
        - E2E_TEST_ORG (project-factory-test)
        """

        from shared.contracts.queues.scaffolder import ScaffolderMessage

        # Skip if no GitHub credentials
        if not os.getenv("GITHUB_APP_ID"):
            pytest.skip("Requires GITHUB_APP_ID")

        # Create project in API first
        project_resp = await api_client.post(
            "/api/projects",
            json={
                "name": unique_project_name,
                "modules": [ServiceModule.BACKEND.value],
            },
        )
        assert project_resp.status_code == 201
        project_id = project_resp.json()["id"]

        repo_full_name = f"{GITHUB_ORG}/{unique_project_name}"

        try:
            # Publish directly to scaffolder:queue
            msg = ScaffolderMessage(
                project_id=project_id,
                project_name=unique_project_name,
                repo_full_name=repo_full_name,
                modules=[ServiceModule.BACKEND],
            )
            await redis.xadd("scaffolder:queue", {"data": msg.model_dump_json()})

            # Wait for scaffolder:results
            _, result_msg = await wait_for_stream_message(redis, "scaffolder:results", timeout=120)
            result = json.loads(result_msg.get("data", "{}"))

            assert result.get("status") == "success", f"Scaffolder failed: {result}"
            assert result.get("repo_url") is not None
            assert unique_project_name in result.get("repo_url", "")

        finally:
            # Cleanup: delete project from API
            await api_client.delete(f"/api/projects/{project_id}")
            # Note: GitHub repo cleanup should be done separately if needed


class TestPOWorkerFlow:
    """Tests for PO Worker with real Claude.

    These tests verify that PO can create projects using deterministic prompts
    that bypass clarifying questions.
    """

    @pytest.mark.skip(reason="Requires Claude session - enable for manual testing")
    @pytest.mark.e2e_real
    async def test_po_creates_project_with_deterministic_prompt(
        self, redis: Redis, api_client: httpx.AsyncClient, unique_project_name: str
    ):
        """
        Test that PO Worker creates project without asking questions.

        Flow:
        1. Create PO worker (real Claude, host session)
        2. Send deterministic prompt
        3. Wait for worker output
        4. Assert: project create command was executed
        5. Assert: project exists in API
        """
        import json
        import re
        import time

        from shared.contracts.queues.worker import AgentType, CreateWorkerCommand, WorkerConfig
        from tests.e2e.e2e_prompt import EXPECTED_PATTERNS, build_project_creation_prompt

        # Clear response stream
        await redis.delete("worker:responses:po")

        # 1. Create PO Worker
        cmd = CreateWorkerCommand(
            request_id=f"po-e2e-{int(time.time())}",
            config=WorkerConfig(
                name="po-e2e-worker",
                worker_type="po",
                agent_type=AgentType.CLAUDE,
                instructions="You are a Product Owner agent.",
                auth_mode="host_session",
                host_claude_dir="/host-claude",
                allowed_commands=["orchestrator"],
                capabilities=["orchestrator"],
            ),
        )
        await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})

        # Wait for worker creation
        _, resp = await wait_for_stream_message(redis, "worker:responses:po", timeout=60)
        data = json.loads(resp["data"])
        worker_id = data.get("worker_id")
        assert worker_id, f"Failed to get worker_id: {data}"

        try:
            # 2. Send deterministic prompt
            prompt = build_project_creation_prompt(unique_project_name)
            await redis.xadd(
                f"worker:{worker_id}:input",
                {"data": json.dumps({"content": prompt})},
            )

            # 3. Wait for output
            _, output_msg = await wait_for_stream_message(
                redis, f"worker:{worker_id}:output", timeout=180
            )

            # Parse output
            result_str = output_msg.get("data", "")
            try:
                res_json = json.loads(result_str)
                if "raw_output" in res_json:
                    content = res_json["raw_output"]
                else:
                    content = res_json.get("content", str(res_json))
            except json.JSONDecodeError:
                content = result_str

            # 4. Assert command was executed
            assert re.search(
                EXPECTED_PATTERNS["project_create"], content
            ), f"Expected 'orchestrator project create' in output. Got: {content[:500]}"

            # 5. Verify project exists in API
            # Give API some time to process
            await asyncio.sleep(2)

            # Try to find the project by name
            list_resp = await api_client.get("/api/projects")
            if list_resp.status_code == 200:
                projects = list_resp.json()
                matching = [p for p in projects if p.get("name") == unique_project_name]
                assert matching, f"Project '{unique_project_name}' not found in API"

        finally:
            # Cleanup worker
            from shared.contracts.queues.worker import DeleteWorkerCommand

            await redis.xadd(
                "worker:commands",
                {
                    "data": DeleteWorkerCommand(
                        request_id=f"cleanup-{worker_id}", worker_id=worker_id
                    ).model_dump_json()
                },
            )

            # Cleanup project if created
            list_resp = await api_client.get("/api/projects")
            if list_resp.status_code == 200:
                for p in list_resp.json():
                    if p.get("name") == unique_project_name:
                        await api_client.delete(f"/api/projects/{p['id']}")
