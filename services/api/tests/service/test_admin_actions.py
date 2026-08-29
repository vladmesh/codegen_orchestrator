"""Service tests for admin action endpoints (#1024).

Tests: send-to-architect, spawn-worker, stop, undeploy, redeploy,
run-e2e, from-repo, delete-secret. Each verifies DB state + Redis message.
"""

from http import HTTPStatus
import json
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from shared.contracts.acceptance import BASELINE_ACCEPTANCE_CRITERIA
from shared.models import Run, WorkAdmissionAudit

TASK_TEST_TELEGRAM_ID = 999000999
TASK_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_DEPLOY_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(scope="module")
async def client():
    import os

    from src.dependencies import close_redis, init_redis
    from src.main import app

    await init_redis()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]},
    ) as c:
        yield c
    await close_redis()


@pytest.fixture(scope="module")
async def redis():
    from src.config import get_settings

    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture(scope="module")
async def _ensure_project(client: AsyncClient):
    """Ensure test user and project exist."""
    resp = await client.get(f"/api/users/by-telegram/{TASK_TEST_TELEGRAM_ID}")
    if resp.status_code == HTTPStatus.NOT_FOUND:
        await client.post(
            "/api/users/",
            json={
                "telegram_id": TASK_TEST_TELEGRAM_ID,
                "username": "test_admin",
                "first_name": "Test",
                "is_admin": True,
            },
        )

    resp = await client.get(f"/api/projects/{TASK_TEST_PROJECT_ID}")
    if resp.status_code == HTTPStatus.NOT_FOUND:
        await client.post(
            "/api/projects/",
            json={
                "id": TASK_TEST_PROJECT_ID,
                "title": "Admin Actions Test",
                "initiating_run_id": "test-run-1",
                "status": "active",
                "config": {},
            },
            headers={"X-Telegram-ID": str(TASK_TEST_TELEGRAM_ID)},
        )


@pytest.fixture(scope="module")
async def server_handle(client: AsyncClient, _ensure_project):
    """Ensure a test server exists, return its handle."""
    handle = "test-admin-server"
    resp = await client.get(f"/api/servers/{handle}")
    if resp.status_code == HTTPStatus.NOT_FOUND:
        await client.post(
            "/api/servers/",
            json={
                "handle": handle,
                "host": "test.example.com",
                "public_ip": "10.0.0.1",
                "ssh_user": "root",
            },
        )
    return handle


async def _read_last_message(redis: Redis, stream: str) -> dict:
    """Read the last message from a Redis stream."""
    msgs = await redis.xrevrange(stream, count=1)
    assert msgs, f"No messages in {stream}"
    _msg_id, fields = msgs[0]
    return json.loads(fields["data"])


@pytest.fixture
def admin_deploy_head_sha(monkeypatch):
    class FakeGitHubClient:
        async def get_default_branch_head_sha(self, owner: str, repo: str) -> str:
            assert owner
            assert repo
            return ADMIN_DEPLOY_HEAD_SHA

    monkeypatch.setattr("src.routers.applications.GitHubAppClient", FakeGitHubClient)
    return ADMIN_DEPLOY_HEAD_SHA


# ---------------------------------------------------------------------------
# send-to-architect
# ---------------------------------------------------------------------------


class TestSendToArchitect:
    @pytest.mark.asyncio
    async def test_send_created_story(self, client, redis, _ensure_project):
        # Create a story
        resp = await client.post(
            "/api/stories/",
            json={
                "project_id": TASK_TEST_PROJECT_ID,
                "title": "Test story for architect",
            },
        )
        assert resp.status_code == HTTPStatus.CREATED
        story_id = resp.json()["id"]

        # Send to architect
        resp = await client.post(
            f"/api/stories/{story_id}/send-to-architect",
            json={"actor": "test"},
        )
        assert resp.status_code == HTTPStatus.OK
        assert resp.json()["status"] == "in_progress"

        # Verify message in architect:queue
        msg = await _read_last_message(redis, "architect:queue")
        assert msg["story_id"] == story_id
        assert msg["is_reopen"] is False

    @pytest.mark.asyncio
    async def test_send_wrong_status_fails(self, client, _ensure_project):
        # Create and start a story (→ in_progress)
        resp = await client.post(
            "/api/stories/",
            json={
                "project_id": TASK_TEST_PROJECT_ID,
                "title": "Wrong status story",
            },
        )
        story_id = resp.json()["id"]
        await client.post(f"/api/stories/{story_id}/start")

        resp = await client.post(f"/api/stories/{story_id}/send-to-architect")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_send_nonexistent_story_404(self, client, _ensure_project):
        resp = await client.post("/api/stories/story-nonexistent/send-to-architect")
        assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# spawn-worker
# ---------------------------------------------------------------------------


class TestSpawnWorker:
    @pytest.mark.asyncio
    async def test_spawn_from_backlog(self, client, redis, _ensure_project):
        # Create a task
        resp = await client.post(
            "/api/tasks/",
            json={
                "project_id": TASK_TEST_PROJECT_ID,
                "title": "Spawn worker test",
                "type": "feature",
            },
        )
        assert resp.status_code == HTTPStatus.CREATED
        task_id = resp.json()["id"]

        # Spawn worker
        resp = await client.post(
            f"/api/tasks/{task_id}/spawn-worker",
            json={"actor": "test", "description": "custom description"},
        )
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert data["task"]["status"] == "in_dev"
        assert data["run"]["type"] == "engineering"
        assert data["run"]["id"].startswith("eng-")

        # Verify message in engineering:queue
        msg = await _read_last_message(redis, "engineering:queue")
        assert msg["planning_task_id"] == task_id
        assert msg["description"] == "custom description"

    @pytest.mark.asyncio
    async def test_spawn_wrong_status_fails(self, client, _ensure_project):
        resp = await client.post(
            "/api/tasks/",
            json={
                "project_id": TASK_TEST_PROJECT_ID,
                "title": "Wrong status spawn",
                "type": "feature",
            },
        )
        task_id = resp.json()["id"]
        # Move to done
        await client.post(f"/api/tasks/{task_id}/start")
        await client.post(f"/api/tasks/{task_id}/complete")

        resp = await client.post(f"/api/tasks/{task_id}/spawn-worker")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_ordinary_lk_bearer_cannot_start_paid_operator_actions(
        self, client, redis, server_handle, _ensure_project, db_session
    ):
        """The global bearer gate is not authority to create paid work."""
        from src.dependencies import create_lk_jwt
        from src.main import app

        ordinary = await client.post(
            "/api/users/",
            json={"telegram_id": uuid.uuid4().int % 1_000_000_000, "username": "ordinary-lk"},
        )
        assert ordinary.status_code == HTTPStatus.CREATED, ordinary.text
        task = await client.post(
            "/api/tasks/",
            json={
                "project_id": TASK_TEST_PROJECT_ID,
                "title": "Unauthorised worker spawn",
                "type": "feature",
            },
        )
        assert task.status_code == HTTPStatus.CREATED, task.text
        app_id = await _create_running_app(client, server_handle)

        before_runs = await db_session.scalar(select(func.count()).select_from(Run))
        before_audits = await db_session.scalar(
            select(func.count()).select_from(WorkAdmissionAudit)
        )
        before_engineering = await redis.xlen("engineering:queue")
        before_qa = await redis.xlen("qa:queue")
        token = create_lk_jwt(ordinary.json()["id"])
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as bearer_client:
            spawned = await bearer_client.post(
                f"/api/tasks/{task.json()['id']}/spawn-worker", json={"actor": "ordinary"}
            )
            e2e = await bearer_client.post(
                f"/api/applications/{app_id}/run-e2e", json={"actor": "ordinary"}
            )

        assert spawned.status_code == HTTPStatus.FORBIDDEN
        assert e2e.status_code == HTTPStatus.FORBIDDEN
        assert await db_session.scalar(select(func.count()).select_from(Run)) == before_runs
        assert (
            await db_session.scalar(select(func.count()).select_from(WorkAdmissionAudit))
            == before_audits
        )
        assert await redis.xlen("engineering:queue") == before_engineering
        assert await redis.xlen("qa:queue") == before_qa


# ---------------------------------------------------------------------------
# stop / undeploy / redeploy
# ---------------------------------------------------------------------------


async def _create_repo(client) -> str:
    """Helper: create a unique repository, return its id."""
    resp = await client.post(
        "/api/repositories/",
        json={
            "project_id": TASK_TEST_PROJECT_ID,
            "name": f"repo-{uuid.uuid4().hex[:6]}",
            "git_url": f"https://github.com/test/{uuid.uuid4().hex[:8]}.git",
        },
    )
    assert resp.status_code == HTTPStatus.CREATED
    return resp.json()["id"]


async def _create_running_app(client, server_handle, app_status="running", repo_id=None):
    """Helper: create an application with a unique repo and a port."""
    rid = repo_id or await _create_repo(client)
    svc_name = f"svc-{uuid.uuid4().hex[:6]}"
    resp = await client.post(
        "/api/applications/",
        json={
            "repo_id": rid,
            "server_handle": server_handle,
            "service_name": svc_name,
            "status": app_status,
        },
    )
    assert resp.status_code == HTTPStatus.CREATED, f"Failed: {resp.text}"
    app_id = resp.json()["id"]

    # Allocate a port
    resp = await client.post(
        f"/api/servers/{server_handle}/ports/allocate-next",
        json={
            "service_name": svc_name,
            "application_id": app_id,
        },
    )
    assert resp.status_code == HTTPStatus.OK, f"Port allocation failed: {resp.text}"
    return app_id


class TestStopApplication:
    @pytest.mark.asyncio
    async def test_stop_running_app(self, client, redis, server_handle):
        app_id = await _create_running_app(client, server_handle)

        resp = await client.post(f"/api/applications/{app_id}/stop", json={"actor": "test"})
        assert resp.status_code == HTTPStatus.OK
        assert resp.json()["status"] == "stopping"

        msg = await _read_last_message(redis, "deploy:queue")
        assert msg["action"] == "stop"
        assert msg["triggered_by"] == "admin"
        # The consumer stops the application it is told about, not one it picks.
        assert msg["application_id"] == app_id

    @pytest.mark.asyncio
    async def test_stop_not_running_fails(self, client, server_handle):
        app_id = await _create_running_app(client, server_handle, app_status="not_deployed")

        resp = await client.post(f"/api/applications/{app_id}/stop")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_stop_already_stopped_is_idempotent(self, client, redis, server_handle):
        app_id = await _create_running_app(client, server_handle, app_status="stopped")
        message_count_before = await redis.xlen("deploy:queue")

        resp = await client.post(f"/api/applications/{app_id}/stop")

        assert resp.status_code == HTTPStatus.OK
        assert resp.json()["status"] == "stopped"
        assert await redis.xlen("deploy:queue") == message_count_before


class TestUndeployApplication:
    @pytest.mark.asyncio
    async def test_undeploy_running_app(self, client, redis, server_handle):
        app_id = await _create_running_app(client, server_handle)

        resp = await client.post(f"/api/applications/{app_id}/undeploy", json={"actor": "test"})
        assert resp.status_code == HTTPStatus.OK
        assert resp.json()["status"] == "undeploying"

        msg = await _read_last_message(redis, "deploy:queue")
        assert msg["action"] == "undeploy"
        assert msg["application_id"] == app_id

    @pytest.mark.asyncio
    async def test_undeploy_not_deployed_fails(self, client, server_handle):
        app_id = await _create_running_app(client, server_handle, app_status="not_deployed")

        resp = await client.post(f"/api/applications/{app_id}/undeploy")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


class TestRedeployApplication:
    @pytest.mark.asyncio
    async def test_redeploy_app(self, client, redis, server_handle, admin_deploy_head_sha):
        app_id = await _create_running_app(client, server_handle)

        resp = await client.post(f"/api/applications/{app_id}/redeploy", json={"actor": "test"})
        assert resp.status_code == HTTPStatus.OK
        assert resp.json()["status"] == "deploying"

        msg = await _read_last_message(redis, "deploy:queue")
        assert msg["action"] == "create"
        assert msg["triggered_by"] == "admin"
        assert msg["head_sha"] == admin_deploy_head_sha

    @pytest.mark.asyncio
    async def test_redeploy_restores_a_quarantined_target_without_creating_qa(
        self, client, redis, server_handle, _ensure_project, admin_deploy_head_sha
    ):
        """A standalone deploy is the recovery route when QA cannot be rechecked."""
        app_id = await _create_running_app(client, server_handle, app_status="stopped")
        story = await client.post(
            "/api/stories/",
            json={"project_id": TASK_TEST_PROJECT_ID, "title": "Restore blocked target"},
        )
        assert story.status_code == HTTPStatus.CREATED, story.text
        story_id = story.json()["id"]
        assert (await client.post(f"/api/stories/{story_id}/start")).status_code == HTTPStatus.OK
        assert (
            await client.post(f"/api/stories/{story_id}/human-review")
        ).status_code == HTTPStatus.OK
        receipt = await client.post(
            "/api/work-admission/paid-runs",
            json={
                "id": f"qa-quarantine-{uuid.uuid4().hex[:12]}",
                "type": "qa",
                "project_id": TASK_TEST_PROJECT_ID,
                "story_id": story_id,
                "run_metadata": {"application_id": app_id},
            },
        )
        assert receipt.status_code == HTTPStatus.OK, receipt.text
        quarantined = await client.patch(
            f"/api/stories/{story_id}",
            json={
                "quarantine_reason": {
                    "qa_outcome": "blocked",
                    "blocker": {"category": "bot_not_live"},
                }
            },
        )
        assert quarantined.status_code == HTTPStatus.OK, quarantined.text

        before = await redis.xlen("deploy:queue")
        restored = await client.post(f"/api/applications/{app_id}/redeploy", json={"actor": "test"})

        assert restored.status_code == HTTPStatus.OK, restored.text
        assert restored.json()["status"] == "deploying"
        assert await redis.xlen("deploy:queue") == before + 1
        message = await _read_last_message(redis, "deploy:queue")
        assert message["application_id"] is None
        assert message["story_id"] == ""
        assert message["head_sha"] == admin_deploy_head_sha

    @pytest.mark.asyncio
    async def test_redeploy_head_sha_failure_does_not_publish(
        self, client, redis, server_handle, monkeypatch
    ):
        class FailingGitHubClient:
            async def get_default_branch_head_sha(self, owner: str, repo: str) -> str:
                raise RuntimeError("github unavailable")

        monkeypatch.setattr("src.routers.applications.GitHubAppClient", FailingGitHubClient)
        before = await redis.xlen("deploy:queue")
        app_id = await _create_running_app(client, server_handle)

        resp = await client.post(f"/api/applications/{app_id}/redeploy", json={"actor": "test"})

        assert resp.status_code == HTTPStatus.BAD_GATEWAY
        assert "Could not resolve head SHA" in resp.text
        assert await redis.xlen("deploy:queue") == before
        app_resp = await client.get(f"/api/applications/{app_id}")
        assert app_resp.json()["status"] == "running"


# ---------------------------------------------------------------------------
# run-e2e
# ---------------------------------------------------------------------------


class TestRunE2E:
    @pytest.mark.asyncio
    async def test_run_e2e_on_running_app(self, client, redis, server_handle):
        app_id = await _create_running_app(client, server_handle)

        resp = await client.post(f"/api/applications/{app_id}/run-e2e", json={"actor": "test"})
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert data["run"]["type"] == "qa"
        assert data["run"]["id"].startswith("qa-")

        msg = await _read_last_message(redis, "qa:queue")
        assert msg["application_id"] == app_id
        assert "10.0.0.1" in msg["deployed_url"]
        # A repository is seeded with criteria at creation, so QA gets something
        # to test without anyone filling the repository in by hand.
        assert msg["acceptance_criteria"] == BASELINE_ACCEPTANCE_CRITERIA

    @pytest.mark.asyncio
    async def test_run_e2e_not_running_fails(self, client, server_handle):
        app_id = await _create_running_app(client, server_handle, app_status="stopped")

        resp = await client.post(f"/api/applications/{app_id}/run-e2e")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_run_e2e_without_criteria_creates_no_run(self, client, server_handle):
        """Criteria cleared → rejected before a Run exists, not a run that can only error."""
        rid = await _create_repo(client)
        app_id = await _create_running_app(client, server_handle, repo_id=rid)

        resp = await client.patch(f"/api/repositories/{rid}", json={"acceptance_criteria": ""})
        assert resp.status_code == HTTPStatus.OK

        resp = await client.post(f"/api/applications/{app_id}/run-e2e", json={"actor": "test"})
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert "acceptance_criteria" in resp.text

        runs = await client.get(f"/api/applications/{app_id}/runs")
        assert runs.json() == []

    @pytest.mark.asyncio
    async def test_run_e2e_recipient_resolution_failure_aborts_paid_run(
        self, client, redis, server_handle, db_session, monkeypatch
    ):
        """A failure before QA publication leaves neither a live Run nor a slot."""
        from unittest.mock import AsyncMock

        from src.routers import applications

        app_id = await _create_running_app(client, server_handle)
        before_messages = await redis.xlen("qa:queue")
        monkeypatch.setattr(
            applications,
            "resolve_project_chat_id",
            AsyncMock(side_effect=RuntimeError("chat resolver unavailable")),
        )

        response = await client.post(f"/api/applications/{app_id}/run-e2e", json={"actor": "test"})

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        runs = (await db_session.scalars(select(Run).where(Run.type == "qa"))).all()
        matching = [run for run in runs if (run.run_metadata or {}).get("application_id") == app_id]
        assert len(matching) == 1
        assert matching[0].status == "cancelled"
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.id == matching[0].id, Run.status.in_(("queued", "running")))
            )
            == 0
        )
        assert await redis.xlen("qa:queue") == before_messages


# ---------------------------------------------------------------------------
# from-repo
# ---------------------------------------------------------------------------


class TestFromRepo:
    @pytest.mark.asyncio
    async def test_create_from_repo(
        self, client, redis, server_handle, _ensure_project, admin_deploy_head_sha
    ):
        repo_url = f"https://github.com/test/from-repo-{uuid.uuid4().hex[:6]}.git"
        resp = await client.post(
            "/api/applications/from-repo",
            json={
                "repo_url": repo_url,
                "project_id": TASK_TEST_PROJECT_ID,
                "server_handle": server_handle,
                "service_name": f"svc-fr-{uuid.uuid4().hex[:6]}",
            },
        )
        assert resp.status_code == HTTPStatus.CREATED
        data = resp.json()
        assert data["application"]["status"] == "deploying"
        assert data["repository"]["git_url"] == repo_url

        msg = await _read_last_message(redis, "deploy:queue")
        assert msg["action"] == "create"
        assert msg["triggered_by"] == "admin"
        assert msg["head_sha"] == admin_deploy_head_sha

    @pytest.mark.asyncio
    async def test_from_repo_bad_server_404(self, client, _ensure_project):
        resp = await client.post(
            "/api/applications/from-repo",
            json={
                "repo_url": "https://github.com/test/some.git",
                "project_id": TASK_TEST_PROJECT_ID,
                "server_handle": "nonexistent-server",
                "service_name": "test",
            },
        )
        assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# delete-secret
# ---------------------------------------------------------------------------


class TestListSecretKeys:
    @pytest.mark.asyncio
    async def test_list_keys_returns_sorted_names(self, client, _ensure_project):
        pid = TASK_TEST_PROJECT_ID

        # Merge secrets
        await client.post(
            f"/api/projects/{pid}/config/secrets",
            json={"secrets": {"Z_KEY": "z", "A_KEY": "a"}},
        )

        resp = await client.get(f"/api/projects/{pid}/config/secrets/keys")
        assert resp.status_code == HTTPStatus.OK
        keys = resp.json()["keys"]
        assert "A_KEY" in keys
        assert "Z_KEY" in keys
        # Values must NOT be exposed
        assert "a" not in str(resp.json())
        assert "z" not in str(resp.json())

    @pytest.mark.asyncio
    async def test_list_keys_empty_project(self, client):
        # Create a fresh project with no secrets
        fresh_pid = str(uuid.uuid4())
        await client.post(
            "/api/projects/",
            json={
                "id": fresh_pid,
                "title": "No secrets project",
                "initiating_run_id": "test-run-1",
                "status": "active",
                "config": {},
            },
            headers={"X-Telegram-ID": str(TASK_TEST_TELEGRAM_ID)},
        )

        resp = await client.get(f"/api/projects/{fresh_pid}/config/secrets/keys")
        assert resp.status_code == HTTPStatus.OK
        assert resp.json()["keys"] == []


class TestDeleteSecret:
    @pytest.mark.asyncio
    async def test_delete_existing_secret(self, client, _ensure_project):
        pid = TASK_TEST_PROJECT_ID

        # Merge 2 secrets
        await client.post(
            f"/api/projects/{pid}/config/secrets",
            json={"secrets": {"KEY_A": "val_a", "KEY_B": "val_b"}},
        )

        # Delete one
        resp = await client.delete(f"/api/projects/{pid}/config/secrets/KEY_A")
        assert resp.status_code == HTTPStatus.OK
        assert "KEY_A" not in resp.json()["keys"]
        assert "KEY_B" in resp.json()["keys"]

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key_404(self, client, _ensure_project):
        resp = await client.delete(f"/api/projects/{TASK_TEST_PROJECT_ID}/config/secrets/NOPE")
        assert resp.status_code == HTTPStatus.NOT_FOUND
