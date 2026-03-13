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
TEST_TELEGRAM_ID = "999000"


@pytest.fixture(autouse=True)
async def ensure_test_user():
    """Ensure the test user exists before running CLI commands."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        resp = await client.get(f"/api/users/by-telegram/{TEST_TELEGRAM_ID}")
        if resp.status_code == 404:
            resp = await client.post(
                "/api/users/upsert",
                json={
                    "telegram_id": int(TEST_TELEGRAM_ID),
                    "username": "cli-test-user",
                },
                headers={"X-Telegram-ID": TEST_TELEGRAM_ID},
            )
            resp.raise_for_status()


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
        env={
            **os.environ,
            "ORCHESTRATOR_API_URL": API_URL,
            "ORCHESTRATOR_TELEGRAM_ID": TEST_TELEGRAM_ID,
        },
    )

    assert result.returncode == 0, f"CLI command failed: {result.stdout}\n{result.stderr}"

    data = json.loads(result.stdout)
    project_id = data["id"]

    # 2. Verify API
    async with httpx.AsyncClient(base_url=API_URL) as client:
        response = await client.get(f"/api/projects/{project_id}")
        assert response.status_code == 200  # noqa: PLR2004
        project = ProjectDTO.model_validate(response.json())
        assert project.name == project_name
        assert project.id == project_id
