"""Unit tests for POST /webhooks/github endpoint."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
import pytest

from src.main import app

SECRET = "test-webhook-secret"  # noqa: S105


def _sign(payload: bytes) -> str:
    sig = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _make_payload(
    *,
    action="completed",
    conclusion="success",
    workflow_path=".github/workflows/ci.yml",
    head_branch="main",
    repo_id=12345,
    head_sha="abc1234567890",
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "workflow_run": {
                "conclusion": conclusion,
                "path": workflow_path,
                "head_branch": head_branch,
                "head_sha": head_sha,
            },
            "repository": {"id": repo_id},
        }
    ).encode()


def _mock_project(*, project_id="proj-1", status="active", owner_id=1, github_repo_id=12345):
    p = MagicMock()
    p.id = project_id
    p.status = status
    p.owner_id = owner_id
    p.github_repo_id = github_repo_id
    return p


def _mock_user(*, user_id=1, telegram_id=99999):
    u = MagicMock()
    u.id = user_id
    u.telegram_id = telegram_id
    return u


@pytest.fixture
def mock_env():
    with patch.dict(
        "os.environ",
        {"GITHUB_WEBHOOK_SECRET": SECRET, "REDIS_URL": "redis://localhost:6379"},
    ):
        yield


@pytest.fixture
def mock_db():
    """Mock the database session dependency."""
    session = AsyncMock()
    return session


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.xadd = AsyncMock()
    r.aclose = AsyncMock()
    return r


async def _post_webhook(payload: bytes, *, event="workflow_run", signature=None):
    if signature is None:
        signature = _sign(payload)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/webhooks/github",
            content=payload,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": event,
                "Content-Type": "application/json",
            },
        )


@pytest.mark.asyncio
async def test_webhook_invalid_signature(mock_env):
    payload = _make_payload()
    resp = await _post_webhook(payload, signature="sha256=invalid")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_webhook_ignores_non_workflow_run_event(mock_env):
    payload = b'{"action": "opened"}'
    sig = _sign(payload)
    resp = await _post_webhook(payload, event="push", signature=sig)
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "event type" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_ignores_deploy_yml(mock_env):
    payload = _make_payload(workflow_path=".github/workflows/deploy.yml")
    resp = await _post_webhook(payload)
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "workflow" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_ignores_non_main_branch(mock_env):
    payload = _make_payload(head_branch="feature/foo")
    resp = await _post_webhook(payload)
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "branch" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_ignores_failed_ci(mock_env):
    payload = _make_payload(conclusion="failure")
    resp = await _post_webhook(payload)
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "conclusion" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_ignores_unknown_repo(mock_env):
    payload = _make_payload(repo_id=99999)

    # Mock DB: no project found
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("src.routers.webhooks.get_async_session", return_value=mock_session):
        # Override the dependency
        from src.database import get_async_session as real_dep

        async def fake_session():
            yield mock_session

        app.dependency_overrides[real_dep] = fake_session
        try:
            resp = await _post_webhook(payload)
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "unknown repository" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_ignores_non_active_project(mock_env):
    payload = _make_payload()

    project = _mock_project(status="deploying")

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = project
    mock_session.execute = AsyncMock(return_value=mock_result)

    from src.database import get_async_session as real_dep

    async def fake_session():
        yield mock_session

    app.dependency_overrides[real_dep] = fake_session
    try:
        resp = await _post_webhook(payload)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "project status" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_ci_success_triggers_deploy(mock_env, mock_redis):
    payload = _make_payload()

    project = _mock_project()
    user = _mock_user()

    mock_session = AsyncMock()
    # First execute call returns project, second returns user
    project_result = MagicMock()
    project_result.scalar_one_or_none.return_value = project
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user
    mock_session.execute = AsyncMock(side_effect=[project_result, user_result])
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    from src.database import get_async_session as real_dep

    async def fake_session():
        yield mock_session

    app.dependency_overrides[real_dep] = fake_session

    with patch("src.routers.webhooks.aioredis.from_url", return_value=mock_redis):
        try:
            resp = await _post_webhook(payload)
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["project_id"] == "proj-1"
    assert data["run_id"].startswith("deploy-wh-")

    # Verify run was added to DB session
    mock_session.add.assert_called_once()
    run_obj = mock_session.add.call_args[0][0]
    assert run_obj.type == "deploy"
    assert run_obj.status == "queued"
    assert run_obj.run_metadata["triggered_by"] == "webhook"

    # Verify Redis xadd was called with {"data": json_string} format
    mock_redis.xadd.assert_called_once()
    call_args = mock_redis.xadd.call_args
    assert call_args[0][0] == "deploy:queue"
    raw_fields = call_args[0][1]
    assert "data" in raw_fields
    deploy_data = json.loads(raw_fields["data"])
    assert deploy_data["project_id"] == "proj-1"
    assert deploy_data["user_id"] == "99999"
    assert deploy_data["triggered_by"] == "webhook"
