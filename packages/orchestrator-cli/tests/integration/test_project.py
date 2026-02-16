import json
import os
import shutil
import subprocess
import time

import httpx
import pytest

from shared.contracts.dto.project import ProjectDTO

# Configuration
API_URL = os.getenv("ORCHESTRATOR_API_URL", "http://api:8000")


@pytest.mark.asyncio
async def test_project_create():
    """
    Integration test for CLI project creation:
    1. Run CLI command to create project
    2. Verify project exists in API
    """
    project_name = f"cli-test-{int(time.time())}"

    # 1. Run CLI command
    orchestrator_cmd = shutil.which("orchestrator")
    if not orchestrator_cmd:
        pytest.fail("orchestrator command not found")

    result = subprocess.run(
        [orchestrator_cmd, "project", "create", "--name", project_name, "--json"],
        capture_output=True,
        text=True,
        env={**os.environ, "ORCHESTRATOR_API_URL": API_URL},
    )

    assert result.returncode == 0, f"CLI command failed: {result.stderr}"

    data = json.loads(result.stdout)
    project_id = data["id"]

    # 2. Verify API
    async with httpx.AsyncClient(base_url=API_URL) as client:
        response = await client.get(f"/api/projects/{project_id}")
        assert response.status_code == 200  # noqa: PLR2004
        project = ProjectDTO.model_validate(response.json())
        assert project.name == project_name
        assert project.id == project_id
