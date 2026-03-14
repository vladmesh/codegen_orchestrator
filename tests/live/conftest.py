"""Shared fixtures for live tests.

Live tests run FROM THE HOST against a running stack (`make up`).
API is accessed via localhost:8000. Redis and internal services
are accessed via `docker compose exec` subprocess calls.

Pipeline helpers (create_noop_project, trigger_scaffold, etc.) are in
pipeline_helpers.py — importable by test modules directly.
"""

import secrets
import subprocess
import uuid

import httpx
import pytest

from shared.contracts.dto.project import ProjectStatus

API_URL = "http://localhost:8000"
TEST_TELEGRAM_ID = 999_000_001
ORCHESTRATOR_ROOT = "/home/vlad/projects/codegen_orchestrator"


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
    """Create a throwaway project, yield it, delete after test."""
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
    yield data
    _cleanup_db(project_id)


def _cleanup_db(project_id: str) -> None:
    """Delete project and all related records via SQL (proper cascade)."""
    sql = (
        f"DELETE FROM task_events WHERE task_id IN "
        f"(SELECT id FROM tasks WHERE project_id = '{project_id}');"
        f"DELETE FROM runs WHERE project_id = '{project_id}';"
        f"DELETE FROM tasks WHERE project_id = '{project_id}';"
        f"DELETE FROM stories WHERE project_id = '{project_id}';"
        f"DELETE FROM brainstorms WHERE project_id = '{project_id}';"
        f"DELETE FROM rag_chunks WHERE project_id = '{project_id}';"
        f"DELETE FROM rag_documents WHERE project_id = '{project_id}';"
        f"DELETE FROM rag_conversation_summaries WHERE project_id = '{project_id}';"
        f"DELETE FROM rag_messages WHERE project_id = '{project_id}';"
        f"DELETE FROM service_deployments WHERE project_id = '{project_id}';"
        f"DELETE FROM applications WHERE repo_id IN "
        f"(SELECT id FROM repositories WHERE project_id = '{project_id}');"
        f"DELETE FROM repositories WHERE project_id = '{project_id}';"
        f"DELETE FROM port_allocations WHERE project_id = '{project_id}';"
        f"DELETE FROM projects WHERE id = '{project_id}';"
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            "orchestrator",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=ORCHESTRATOR_ROOT,
    )
