"""Shared fixtures for live tests.

Live tests run FROM THE HOST against a running stack (`make up`).
API is accessed via localhost:8000. Redis and internal services
are accessed via `docker compose exec` subprocess calls.

Pipeline helpers (create_noop_project, trigger_scaffold, etc.) are in
pipeline_helpers.py — importable by test modules directly.
"""

from pathlib import Path
import secrets
import subprocess
import uuid

import httpx
from live_harness import OwnershipManifest, cleanup_guard, resolve_repo_root
from pipeline_helpers import cleanup_all
import pytest

from shared.contracts.dto.project import ProjectStatus

API_URL = "http://localhost:8000"
TEST_TELEGRAM_ID = 999_000_001
ORCHESTRATOR_ROOT = resolve_repo_root(Path(__file__))


@pytest.fixture
async def api():
    """Async httpx client with test user auth header."""
    headers = {"X-Telegram-ID": str(TEST_TELEGRAM_ID)}
    async with httpx.AsyncClient(base_url=API_URL, timeout=10, headers=headers) as client:
        await client.post(
            "/api/users/upsert",
            json={
                "telegram_id": TEST_TELEGRAM_ID,
                "username": "live_test_bot",
                "first_name": "Live",
                "last_name": "Test",
            },
        )
        yield client


@pytest.fixture
async def api_no_auth():
    """Async httpx client WITHOUT auth header (for health checks etc.)."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        yield client


def _compose_exec(service: str, cmd: str, timeout: int = 10) -> str:
    """Run a command inside a docker compose service, return stdout."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", service, *cmd.split()],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ORCHESTRATOR_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"compose exec {service} {cmd!r} failed: {result.stderr}")
    return result.stdout.strip()


@pytest.fixture(scope="session")
def compose_exec():
    """Helper to run commands inside docker compose services."""
    return _compose_exec


def redis_cli(*args: str) -> str:
    """Shortcut: run redis-cli inside the redis container."""
    cmd = "redis-cli " + " ".join(args)
    return _compose_exec("redis", cmd)


@pytest.fixture(scope="session")
def redis():
    """Redis CLI helper bound to the compose redis service."""
    return redis_cli


@pytest.fixture
async def test_project(api):
    """Create a manifest-owned project and prove its teardown."""
    data, ctx = await create_test_project_context(api)
    async with cleanup_guard(lambda: cleanup_all(api, None, ctx), manifest=ctx["manifest"]):
        yield data


async def create_test_project_context(api):
    """Create the common live project context with immediate ownership."""
    project_id = str(uuid.uuid4())
    resp = await api.post(
        "/api/projects/",
        json={
            "id": project_id,
            "name": f"live-test-{secrets.token_hex(4)}",
            "status": ProjectStatus.DRAFT,
            "config": {"description": "live test project"},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    manifest = OwnershipManifest(project_id)
    manifest.own("project", project_id)
    manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{project_id}.json")
    return data, {"project_id": project_id, "manifest": manifest}
