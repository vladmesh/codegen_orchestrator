import os
import shutil
import subprocess
import time

import httpx
import pytest
import redis.asyncio as redis

from shared.contracts.dto.project import ProjectDTO

# Configuration
API_URL = os.getenv("ORCHESTRATOR_API_URL", "http://api:8000")
REDIS_URL = os.getenv("ORCHESTRATOR_REDIS_URL", "redis://redis:6379")


@pytest.mark.asyncio
async def test_e2e_project_create():
    """
    Full E2E test:
    1. Run CLI command to create project
    2. Verify project exists in API
    3. Verify message published to Redis
    """
    project_name = f"e2e-test-{int(time.time())}"

    # 1. Run CLI command
    # We use the installed 'orchestrator' command
    orchestrator_cmd = shutil.which("orchestrator")
    if not orchestrator_cmd:
        pytest.fail("orchestrator command not found")

    result = subprocess.run(
        [orchestrator_cmd, "project", "create", "--name", project_name, "--json"],
        capture_output=True,
        text=True,
        env={**os.environ, "ORCHESTRATOR_API_URL": API_URL, "ORCHESTRATOR_REDIS_URL": REDIS_URL},
    )

    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)

    assert result.returncode == 0, f"CLI command failed: {result.stderr}"

    # Parse output to get ID (if JSON)
    # The command should output JSON if --json is passed
    import json

    try:
        data = json.loads(result.stdout)
        project_id = data["id"]
    except json.JSONDecodeError:
        pytest.fail(f"Failed to parse JSON output: {result.stdout}")

    # 2. Verify API
    async with httpx.AsyncClient(base_url=API_URL) as client:
        response = await client.get(f"/api/projects/{project_id}")
        assert response.status_code == 200  # noqa: PLR2004
        project = ProjectDTO.model_validate(response.json())
        assert project.name == project_name
        assert project.id == project_id

    # 3. Verify Redis
    r = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        # Read from stream. We might need to handle offset.
        # Assuming we are the first consumer or we read from beginning.
        # Since this is a fresh test environment (test-clean), we can read all.
        streams = await r.xread({"scaffolder:queue": "0-0"}, count=100)

        found = False
        for stream_name, messages in streams:
            if stream_name == "scaffolder:queue":
                for _msg_id, payload in messages:
                    if payload.get("project_id") == project_id:
                        found = True
                        assert payload["name"] == project_name
                        assert payload["action"] == "create"
                        break

        assert found, f"Message for project {project_id} not found in Redis stream"

    finally:
        await r.aclose()
