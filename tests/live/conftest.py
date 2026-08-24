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
from pipeline_helpers import (
    api_client_as_internal_service,
    api_client_as_test_user,
    cleanup_all,
    require_internal_api_key,
)
import pytest

from shared.contracts.dto.project import ProjectStatus
from shared.live_contour import current_contour

API_URL = "http://localhost:8000"
TEST_TELEGRAM_ID = 999_000_001
ORCHESTRATOR_ROOT = resolve_repo_root(Path(__file__))


NO_API_CREDENTIAL_MARKER = "needs_no_api_credential"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{NO_API_CREDENTIAL_MARKER}: builds no client against the live API — it drives the "
        "harness against fakes, or against Redis alone — so the run needs no INTERNAL_API_KEY",
    )


def pytest_collection_modifyitems(session, config, items):
    """Refuse to start a run that will call the live API without a credential.

    Every harness client carries the internal key, so a missing variable is a
    certain failure. Said here — after collection, before the first test — it
    names the variable; discovered at the first request it is a KeyError, or,
    once it silently was not sent, a 401 from somewhere in the middle of a
    30-minute mega run.

    Scoped to runs that reach the API. `tests/live/` also holds regressions that
    reach it never: the offline group drives these same helpers against fakes,
    and the Redis cleanup regression talks only to a container. CI's fast-checks
    runs both with no key in the environment, so demanding it for the whole
    session aborted that job before collecting a test. Needing the API is the
    default and the exception is declared, so a module that forgets the marker
    fails loudly rather than quietly losing the guard.
    """
    if any(item.get_closest_marker(NO_API_CREDENTIAL_MARKER) is None for item in items):
        require_internal_api_key()


@pytest.fixture
async def api():
    """The harness as the bot sees a user: the internal key, and the user named.

    The key is not decoration and not a way past the ownership rules — a request
    that names a user is judged as that user (`resolve_actor`). It is how every
    real caller reaches the API, and since every route under /api requires a
    credential, a client carrying `X-Telegram-ID` alone is now answered 401.
    """
    async with api_client_as_test_user() as client:
        resp = await client.post(
            "/api/users/upsert",
            json={
                "telegram_id": TEST_TELEGRAM_ID,
                "username": "live_test_bot",
                "first_name": "Live",
                "last_name": "Test",
            },
        )
        resp.raise_for_status()
        yield client


@pytest.fixture
async def api_no_auth():
    """Async httpx client with NO credential at all — for /health, which needs none.

    Not one of the three authenticated kinds: every route under /api answers this
    client 401. To reach an internal endpoint without naming a user, use
    `api_internal` or `pipeline_helpers.api_client_as_unscoped_observer`.
    """
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        yield client


@pytest.fixture
async def api_internal():
    """Async httpx client authenticated as an internal service.

    Server, ssh-key and allocation endpoints are gated by require_internal_or_admin,
    exactly as production consumers reach them. Use this client for those, not
    api_no_auth, which they answer with 401.
    """
    async with api_client_as_internal_service() as client:
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
async def test_project(api, api_internal):
    """Create a manifest-owned project and prove its teardown."""
    data, ctx = await create_test_project_context(api)
    async with cleanup_guard(
        lambda: cleanup_all(api_internal, None, ctx), manifest=ctx["manifest"]
    ):
        yield data


async def create_test_project_context(api):
    """Create the common live project context with immediate ownership.

    The run exists before the project does: this run's id is minted here and
    handed to the platform as the project's `initiating_run_id`, which is the
    one place a run identity enters the system. Every worker this run causes —
    developer or QA — is stamped with it at creation, so `docker ps -a --filter
    label=com.codegen.run.id=<manifest.run_id>` answers for this run alone, and
    still answers once the workers are dead.
    """
    project_id = str(uuid.uuid4())
    manifest = OwnershipManifest(f"live-{uuid.uuid4().hex[:12]}")
    resp = await api.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": f"{current_contour().pipeline}-{secrets.token_hex(4)}",
            "initiating_run_id": manifest.run_id,
            "status": ProjectStatus.DRAFT,
            "config": {"description": "live test project"},
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201, resp.text
    data = resp.json()
    manifest.own("project", project_id)
    manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{manifest.run_id}.json")
    return data, {"project_id": project_id, "manifest": manifest}
