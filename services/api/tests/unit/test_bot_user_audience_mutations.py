"""Conversational bot-audience mutations: add/remove one Telegram ID atomically.

`set_bot_access` replaces the whole audience and is right for the initial
choice, but a conversation says "add user 84" — and a tool that makes the LLM
reconstruct a comma-separated list can silently drop the other IDs. These tests
pin the typed mutation endpoints: atomic under the project row lock, numeric
validation, deduplication, no accidental public bot, and a config-only rollout
of the already-deployed commit instead of an engineering story.
"""

from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from src.database import get_async_session
from src.dependencies import get_redis_client
from src.main import app

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEPLOYED_SHA = "abc1234567890abc1234567890abc1234567890a"
HTTP_UNPROCESSABLE = 422
HTTP_CONFLICT = 409


def _project(config: dict) -> MagicMock:
    project = MagicMock()
    project.id = PROJECT_UUID
    project.config = config
    return project


def _locked_project_result(project):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=project)
    return result


def _audience_private():
    return {
        "bot_access": {"mode": "only_me", "allowed_telegram_ids": "42"},
        "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": "42"},
    }


class _ScriptedSession:
    """AsyncSession stand-in answering each execute() from a list of results.

    Results run out when the handler makes more queries than the test scripted;
    the tail answers with an empty result, which is what "no rows" means here.
    """

    def __init__(self, results):
        self._results = list(results)
        self.executed = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.refresh = AsyncMock()

    def _empty(self):
        result = MagicMock()
        result.first.return_value = None
        result.all.return_value = []
        result.scalar_one_or_none.return_value = None
        return result

    async def execute(self, statement, *args, **kwargs):
        self.executed.append(str(statement))
        if self._results:
            return self._results.pop(0)
        return self._empty()

    async def get(self, *args, **kwargs):
        return None

    def add(self, *args, **kwargs):
        pass


def _client(session, redis=None):
    redis = redis or MagicMock()
    app.dependency_overrides[get_async_session] = lambda: session
    app.dependency_overrides[get_redis_client] = lambda: redis
    return session, redis


def _drop_overrides():
    app.dependency_overrides.pop(get_async_session, None)
    app.dependency_overrides.pop(get_redis_client, None)


class TestAddBotUser:
    @pytest.mark.asyncio
    async def test_add_preserves_existing_ids_and_deduplicates(self):
        """Adding to "42" yields "42,84"; adding an existing ID changes nothing."""
        project = _project(_audience_private())
        # Two requests: the first mutates (project read + deployment lookup),
        # the second is the idempotent repeat (project read only, no write).
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                _locked_project_result(project),
                _locked_project_result(project),
            ]
        )
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                with patch_recipient("111"):
                    added = await c.post(
                        f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                        json={"telegram_id": 84},
                        headers={"X-Internal-Key": "test-internal-key"},
                    )
                    duplicate = await c.post(
                        f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                        json={"telegram_id": 84},
                        headers={"X-Internal-Key": "test-internal-key"},
                    )
        finally:
            _drop_overrides()

        assert added.status_code == 200, added.text
        assert added.json()["audience"] == "42,84"
        assert added.json()["operation"] == "added"
        assert project.config["env_overrides"]["TG_BOT_ALLOWED_TELEGRAM_IDS"] == "42,84"
        assert project.config["bot_access"]["allowed_telegram_ids"] == "42,84"

        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["operation"] == "already_present"
        assert duplicate.json()["audience"] == "42,84"
        assert project.config["env_overrides"]["TG_BOT_ALLOWED_TELEGRAM_IDS"] == "42,84"

    @pytest.mark.asyncio
    async def test_add_locks_the_project_row(self):
        """The mutation reads the audience under the same FOR UPDATE lock as every
        other config writer, so two concurrent adds cannot lose an ID."""
        project = _project(_audience_private())
        session = _ScriptedSession([_locked_project_result(project), MagicMock(first=lambda: None)])
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                await c.post(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                    json={"telegram_id": 84},
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

        assert "FOR UPDATE" in session.executed[0]

    @pytest.mark.asyncio
    async def test_add_rejects_non_numeric_ids(self):
        project = _project(_audience_private())
        session = _ScriptedSession([_locked_project_result(project)])
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                for bad in (0, -5):
                    resp = await c.post(
                        f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                        json={"telegram_id": bad},
                        headers={"X-Internal-Key": "test-internal-key"},
                    )
                    assert resp.status_code == HTTP_UNPROCESSABLE
        finally:
            _drop_overrides()

    @pytest.mark.asyncio
    async def test_add_to_a_public_bot_is_refused(self):
        """A public bot has no audience to extend; going private is set_bot_access's
        explicit decision, not a side effect of adding one user."""
        project = _project({"bot_access": {"mode": "public", "allowed_telegram_ids": ""}})
        session = _ScriptedSession([_locked_project_result(project)])
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.post(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                    json={"telegram_id": 84},
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

        assert resp.status_code == HTTP_UNPROCESSABLE
        assert "public" in resp.json()["detail"] or "private" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_add_without_a_chosen_audience_is_refused(self):
        project = _project({})
        session = _ScriptedSession([_locked_project_result(project)])
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.post(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                    json={"telegram_id": 84},
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

        assert resp.status_code == HTTP_UNPROCESSABLE


class TestRemoveBotUser:
    @pytest.mark.asyncio
    async def test_remove_preserves_the_other_ids(self):
        project = _project(
            {
                "bot_access": {"mode": "custom", "allowed_telegram_ids": "42,84"},
                "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": "42,84"},
            }
        )
        session = _ScriptedSession([_locked_project_result(project), MagicMock(first=lambda: None)])
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.delete(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access/users/84",
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

        assert resp.status_code == 200, resp.text
        assert resp.json()["operation"] == "removed"
        assert resp.json()["audience"] == "42"
        assert project.config["env_overrides"]["TG_BOT_ALLOWED_TELEGRAM_IDS"] == "42"

    @pytest.mark.asyncio
    async def test_removing_the_last_id_cannot_make_the_bot_public(self):
        project = _project(_audience_private())
        session = _ScriptedSession([_locked_project_result(project)])
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.delete(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access/users/42",
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

        assert resp.status_code == HTTP_UNPROCESSABLE
        assert "public" in resp.json()["detail"]
        assert project.config["env_overrides"]["TG_BOT_ALLOWED_TELEGRAM_IDS"] == "42"

    @pytest.mark.asyncio
    async def test_removing_an_absent_id_is_an_idempotent_no_op(self):
        project = _project(_audience_private())
        session = _ScriptedSession([_locked_project_result(project)])
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.delete(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access/users/999",
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

        assert resp.status_code == 200, resp.text
        assert resp.json()["operation"] == "already_absent"
        assert resp.json()["audience"] == "42"
        session.commit.assert_not_called()


class TestConfigOnlyRollout:
    @pytest.mark.asyncio
    async def test_deployed_project_launches_one_rollout_on_the_deployed_sha(self):
        """A live bot gets a config-only redeploy of its recorded SHA: no story,
        no engineering, and the audience travels in the project config the deploy
        resolver already reads."""
        project = _project(_audience_private())
        deployment = MagicMock(application_id=7, deployed_sha=DEPLOYED_SHA, deployed_at=1)
        target_row = MagicMock()
        target_row.all = MagicMock(return_value=[(7, deployment)])
        session = _ScriptedSession([_locked_project_result(project), target_row])
        redis = MagicMock()
        redis.publish_message = AsyncMock()
        _client(session, redis)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                with patch_recipient("111"):
                    resp = await c.post(
                        f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                        json={"telegram_id": 84},
                        headers={"X-Internal-Key": "test-internal-key"},
                    )
        finally:
            _drop_overrides()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rollout"] == "pending"
        assert body["rollout_run_id"]

        redis.publish_message.assert_awaited_once()
        msg = redis.publish_message.await_args.args[1]
        assert isinstance(msg, DeployMessage)
        assert msg.action == DeployAction.FEATURE
        assert msg.head_sha == DEPLOYED_SHA
        assert msg.triggered_by == DeployTrigger.PO
        assert msg.story_id == ""
        assert msg.project_id == str(PROJECT_UUID)
        assert msg.telegram_chat_id == "111"
        # The audience is project config, not a per-deploy override.
        assert msg.env_overrides == {}
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_deployed_project_persists_the_config_without_pretending(self):
        project = _project(_audience_private())
        session = _ScriptedSession([_locked_project_result(project), MagicMock(first=lambda: None)])
        redis = MagicMock()
        redis.publish_message = AsyncMock()
        _client(session, redis)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                with patch_recipient("111"):
                    resp = await c.post(
                        f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                        json={"telegram_id": 84},
                        headers={"X-Internal-Key": "test-internal-key"},
                    )
        finally:
            _drop_overrides()

        assert resp.status_code == 200, resp.text
        assert resp.json()["rollout"] == "not_deployed"
        assert resp.json()["rollout_run_id"] is None
        redis.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_deployment_without_a_recorded_sha_cannot_roll_out(self):
        project = _project(_audience_private())
        deployment = MagicMock(deployed_sha=None, deployed_at=1)
        target_row = MagicMock()
        target_row.all = MagicMock(return_value=[(7, deployment)])
        session = _ScriptedSession([_locked_project_result(project), target_row])
        redis = MagicMock()
        redis.publish_message = AsyncMock()
        _client(session, redis)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                with patch_recipient("111"):
                    resp = await c.post(
                        f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                        json={"telegram_id": 84},
                        headers={"X-Internal-Key": "test-internal-key"},
                    )
        finally:
            _drop_overrides()

        assert resp.status_code == HTTP_CONFLICT
        redis.publish_message.assert_not_called()


class TestRolloutStatus:
    def _run(self, status: str, result: dict | None):
        run = MagicMock()
        run.id = "botrollout-abc"
        run.status = status
        run.result = result
        run.error_message = None
        return run

    async def _get(self, session):
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                return await c.get(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access/rollouts/botrollout-abc",
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

    @pytest.mark.asyncio
    async def test_completed_success_reads_applied(self):
        run_result = MagicMock()
        run_result.scalars.return_value.first = MagicMock(
            return_value=self._run("completed", {"deploy_outcome": "success"})
        )
        session = _ScriptedSession([run_result])
        resp = await self._get(session)

        assert resp.status_code == 200, resp.text
        assert resp.json()["rollout"] == "applied"

    @pytest.mark.asyncio
    async def test_failed_run_reads_failed_with_detail(self):
        run = self._run("failed", {"deploy_outcome": "retry"})
        run.error_message = "deploy workflow failed"
        run_result = MagicMock()
        run_result.scalars.return_value.first = MagicMock(return_value=run)
        session = _ScriptedSession([run_result])
        resp = await self._get(session)

        assert resp.status_code == 200, resp.text
        assert resp.json()["rollout"] == "failed"
        assert "deploy workflow failed" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_running_run_reads_pending(self):
        run_result = MagicMock()
        run_result.scalars.return_value.first = MagicMock(return_value=self._run("running", None))
        session = _ScriptedSession([run_result])
        resp = await self._get(session)

        assert resp.status_code == 200, resp.text
        assert resp.json()["rollout"] == "pending"

    @pytest.mark.asyncio
    async def test_unknown_run_is_404(self):
        run_result = MagicMock()
        run_result.scalars.return_value.first = MagicMock(return_value=None)
        session = _ScriptedSession([run_result])
        resp = await self._get(session)

        assert resp.status_code == 404


def patch_recipient(chat_id: str):
    """Pin the owner-notification resolution the rollout endpoint performs."""
    from unittest.mock import patch

    target = "src.routers.projects.resolve_project_recipient"
    return patch(
        target,
        new=AsyncMock(
            return_value=__import__(
                "src.routers._recipients", fromlist=["ProjectRecipient"]
            ).ProjectRecipient(telegram_chat_id=chat_id)
        ),
    )
