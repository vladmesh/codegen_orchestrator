"""Unit tests for POST /webhooks/github endpoint."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from shared.contracts.dto.project import ProjectStatus
from src.main import app

SECRET = "test-webhook-secret"  # noqa: S105

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


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


def _mock_repository(*, provider_repo_id=12345, project_id=PROJECT_UUID):
    r = MagicMock()
    r.provider_repo_id = provider_repo_id
    r.project_id = project_id
    return r


def _mock_project(*, project_id=PROJECT_UUID, status="active", owner_id=1):
    p = MagicMock()
    p.id = project_id
    p.status = status
    p.owner_id = owner_id
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

    # Mock DB session: Repository lookup returns None
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("src.routers.webhooks.get_async_session", return_value=mock_session):
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
async def test_webhook_pr_merged_non_active_project_ignored(mock_env):
    """PR merge for non-active project → ignored."""
    payload = _make_pr_payload()

    repo = _mock_repository()
    project = _mock_project(status=ProjectStatus.PAUSED.value)

    mock_story = MagicMock()
    mock_story.id = "story-abc123"

    mock_session = AsyncMock()
    repo_result = MagicMock()
    repo_result.scalar_one_or_none.return_value = repo
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = mock_story
    mock_session.execute = AsyncMock(side_effect=[repo_result, story_result])
    mock_session.get = AsyncMock(return_value=project)

    from src.database import get_async_session as real_dep

    async def fake_session():
        yield mock_session

    app.dependency_overrides[real_dep] = fake_session
    try:
        resp = await _post_webhook(payload, event="pull_request")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "project status" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_ci_success_on_main_ignored(mock_env):
    """CI success on main no longer triggers deploy — PR merge handles it."""
    payload = _make_payload()

    repo = _mock_repository()
    project = _mock_project()

    mock_session = AsyncMock()
    repo_result = MagicMock()
    repo_result.scalar_one_or_none.return_value = repo
    mock_session.execute = AsyncMock(return_value=repo_result)
    mock_session.get = AsyncMock(return_value=project)

    from src.database import get_async_session as real_dep

    async def fake_session():
        yield mock_session

    app.dependency_overrides[real_dep] = fake_session
    try:
        resp = await _post_webhook(payload)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["status"] == "ignored"
    assert "PR merge" in data["reason"]


# --- PR merge event tests ---


def _make_pr_payload(
    *,
    action="closed",
    merged=True,
    head_ref="story/story-abc123",
    base_ref="main",
    repo_id=12345,
    head_sha="def456",
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "pull_request": {
                "merged": merged,
                "head": {"ref": head_ref, "sha": head_sha},
                "base": {"ref": base_ref},
                "number": 42,
                "title": "Story: test feature",
            },
            "repository": {"id": repo_id},
        }
    ).encode()


@pytest.mark.asyncio
async def test_webhook_pr_merged_story_branch_triggers_deploy(mock_env, mock_redis):
    """Merged PR from story/* branch → deploy triggered."""
    payload = _make_pr_payload()

    repo = _mock_repository()
    project = _mock_project()
    user = _mock_user()

    # Mock story lookup
    mock_story = MagicMock()
    mock_story.id = "story-abc123"
    mock_story.project_id = PROJECT_UUID
    mock_story.status = "pr_review"

    mock_session = AsyncMock()
    repo_result = MagicMock()
    repo_result.scalar_one_or_none.return_value = repo
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = mock_story
    mock_session.execute = AsyncMock(side_effect=[repo_result, story_result, user_result])
    mock_session.get = AsyncMock(return_value=project)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    from src.database import get_async_session as real_dep

    async def fake_session():
        yield mock_session

    app.dependency_overrides[real_dep] = fake_session

    with (
        patch("src.routers.webhooks.aioredis.from_url", return_value=mock_redis),
        patch("src.routers.webhooks._transition_story_via_api", new_callable=AsyncMock),
    ):
        try:
            resp = await _post_webhook(payload, event="pull_request")
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["status"] == "accepted"
    assert "story_id" in data
    assert data["story_id"] == "story-abc123"

    # Verify deploy message sent
    mock_redis.xadd.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_pr_closed_not_merged_ignored(mock_env):
    """PR closed without merge → ignored."""
    payload = _make_pr_payload(merged=False)
    resp = await _post_webhook(payload, event="pull_request")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "not merged" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_pr_merged_non_story_branch_ignored(mock_env):
    """PR from non-story branch → ignored."""
    payload = _make_pr_payload(head_ref="feature/something")
    resp = await _post_webhook(payload, event="pull_request")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "non-story" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_pr_merged_non_main_base_ignored(mock_env):
    """PR not targeting main → ignored."""
    payload = _make_pr_payload(base_ref="develop")
    resp = await _post_webhook(payload, event="pull_request")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
    assert "base" in resp.json()["reason"]


# --- CI failure on story branch tests ---


@pytest.mark.asyncio
async def test_webhook_ci_failure_story_branch_creates_fix_task(mock_env):
    """CI failure on story/* branch → creates fix task, transitions story."""
    payload = _make_payload(conclusion="failure", head_branch="story/story-abc123")

    repo = _mock_repository()
    project = _mock_project()

    mock_story = MagicMock()
    mock_story.id = "story-abc123"
    mock_story.project_id = PROJECT_UUID
    mock_story.status = "pr_review"

    mock_session = AsyncMock()
    repo_result = MagicMock()
    repo_result.scalar_one_or_none.return_value = repo
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = mock_story
    mock_session.execute = AsyncMock(side_effect=[repo_result, story_result])
    mock_session.get = AsyncMock(return_value=project)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_api_resp = AsyncMock()
    mock_api_resp.status_code = 200
    mock_api_resp.json.return_value = {"id": "task-fix", "status": "todo"}

    from src.database import get_async_session as real_dep

    async def fake_session():
        yield mock_session

    app.dependency_overrides[real_dep] = fake_session

    with (
        patch("src.routers.webhooks.httpx.AsyncClient") as mock_httpx_cls,
        patch("src.routers.webhooks._transition_story_via_api", new_callable=AsyncMock),
    ):
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_api_resp
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls.return_value = mock_http
        try:
            resp = await _post_webhook(payload)
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["status"] == "ci_fix_created"
    assert data["story_id"] == "story-abc123"


@pytest.mark.asyncio
async def test_webhook_ci_failure_main_branch_ignored(mock_env):
    """CI failure on main branch → ignored (same as before)."""
    payload = _make_payload(conclusion="failure", head_branch="main")
    resp = await _post_webhook(payload)
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "ignored"
