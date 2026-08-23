"""Conversational bot-audience mutations: add/remove one Telegram ID atomically.

`set_bot_access` replaces the whole audience and is right for the initial
choice, but a conversation says "add user 84" — and a tool that makes the LLM
reconstruct a comma-separated list can silently drop the other IDs. These tests
pin handler wiring with mocked sessions; the real-SQL guarantees (cross-project
isolation, cross-owner denial, durable publish intent) live in
tests/service/test_bot_audience_rollouts.py.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from shared.contracts.bot_rollout import (
    BOT_ROLLOUT_METADATA_KEY,
    BotRolloutPublishState,
    BotRolloutRecord,
)
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
    project.owner_id = 1
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
        self.added = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.refresh = AsyncMock()

    def _empty(self):
        result = MagicMock()
        result.first.return_value = None
        result.all.return_value = []
        result.scalars.return_value.first.return_value = None
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
        self.added.extend(args)


def _client(session, redis=None):
    redis = redis or MagicMock()
    app.dependency_overrides[get_async_session] = lambda: session
    app.dependency_overrides[get_redis_client] = lambda: redis
    return session, redis


def _drop_overrides():
    app.dependency_overrides.pop(get_async_session, None)
    app.dependency_overrides.pop(get_redis_client, None)


@contextlib.contextmanager
def patch_recipient(chat_id: str):
    """Pin the owner-notification resolution the rollout staging performs."""
    from src.routers._recipients import ProjectRecipient

    with patch(
        "src.routers._bot_access.resolve_project_recipient",
        new=AsyncMock(return_value=ProjectRecipient(telegram_chat_id=chat_id)),
    ):
        yield


def _target_row(application_id: int, sha: str | None):
    """A scripted answer to find_live_rollout_target's `.first()` query."""
    result = MagicMock()
    result.first = MagicMock(return_value=(application_id, sha) if sha is not None else None)
    return result


def _no_target():
    """A scripted empty answer for the live-target lookup."""
    result = MagicMock()
    result.first.return_value = None
    return result


def _running_without_sha_answer(absent: bool):
    """A scripted scalar_one_or_none answer for find_running_without_recorded_sha."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None if absent else 7
    return result


class TestAddBotUser:
    @pytest.mark.asyncio
    async def test_add_preserves_existing_ids_and_deduplicates(self):
        """Adding to "42" yields "42,84"; adding an existing ID changes nothing."""
        project = _project(_audience_private())
        # Request 1 mutates: locked read + target lookup + running-without-SHA
        # check + recipient resolution. Request 2 is the idempotent repeat:
        # locked read only, no write.
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                _no_target(),
                _running_without_sha_answer(absent=True),
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
        assert duplicate.json()["rollout"] == "not_deployed"
        assert project.config["env_overrides"]["TG_BOT_ALLOWED_TELEGRAM_IDS"] == "42,84"

    @pytest.mark.asyncio
    async def test_add_locks_the_project_row(self):
        """The mutation reads the audience under the same FOR UPDATE lock as every
        other config writer, so two concurrent adds cannot lose an ID."""
        project = _project(_audience_private())
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                _no_target(),
                _running_without_sha_answer(absent=True),
            ]
        )
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
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                _no_target(),
                _running_without_sha_answer(absent=True),
            ]
        )
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
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                # find_live_rollout_target returns this application and SHA.
                _target_row(7, DEPLOYED_SHA),
            ]
        )
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
        run = session.added[0]
        record = BotRolloutRecord.model_validate(run.run_metadata[BOT_ROLLOUT_METADATA_KEY])
        assert record.publish is BotRolloutPublishState.PUBLISH_OWED
        assert record.head_sha == DEPLOYED_SHA

    @pytest.mark.asyncio
    async def test_non_deployed_project_persists_the_config_without_pretending(self):
        project = _project(_audience_private())
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                _no_target(),
                _running_without_sha_answer(absent=True),
            ]
        )
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
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                _no_target(),
                _running_without_sha_answer(absent=False),
            ]
        )
        redis = MagicMock()
        redis.publish_message = AsyncMock()
        _client(session, redis)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.post(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access/users",
                    json={"telegram_id": 84},
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

        assert resp.status_code == HTTP_CONFLICT
        redis.publish_message.assert_not_called()


def _run_query_result(run):
    """A scripted answer for rollout_status's run lookup (scalar_one_or_none)."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=run)
    return result


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
        project = _project({})
        session = _ScriptedSession(
            [
                _run_query_result(self._run("completed", {"deploy_outcome": "success"})),
                _locked_project_result(project),
            ]
        )
        resp = await self._get(session)

        assert resp.status_code == 200, resp.text
        assert resp.json()["rollout"] == "applied"

    @pytest.mark.asyncio
    async def test_failed_run_reads_failed_with_detail(self):
        project = _project({})
        run = self._run("failed", {"deploy_outcome": "retry"})
        run.error_message = "deploy workflow failed"
        session = _ScriptedSession([_run_query_result(run), _locked_project_result(project)])
        resp = await self._get(session)

        assert resp.status_code == 200, resp.text
        assert resp.json()["rollout"] == "failed"
        assert "deploy workflow failed" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_running_run_reads_pending(self):
        project = _project({})
        session = _ScriptedSession(
            [
                _run_query_result(self._run("running", None)),
                _locked_project_result(project),
            ]
        )
        resp = await self._get(session)

        assert resp.status_code == 200, resp.text
        assert resp.json()["rollout"] == "pending"

    @pytest.mark.asyncio
    async def test_unknown_run_is_404(self):
        session = _ScriptedSession([_run_query_result(None)])
        resp = await self._get(session)

        assert resp.status_code == 404


class TestSetBotAccessIdempotent:
    """A whole-audience set that matches the stored state is a no-op.

    This path used to raise an unhandled ValueError after the idempotency
    check — a repeat of the same set_bot_access call returned HTTP 500.
    """

    async def _post(self, session, payload):
        _client(session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                return await c.post(
                    f"/api/projects/{PROJECT_UUID}/config/bot-access",
                    json=payload,
                    headers={"X-Internal-Key": "test-internal-key"},
                )
        finally:
            _drop_overrides()

    @pytest.mark.asyncio
    async def test_repeating_the_same_set_succeeds_without_writing(self):
        project = _project(_audience_private())
        # One locked read; nothing else is needed when nothing changes.
        session = _ScriptedSession([_locked_project_result(project)])

        resp = await self._post(session, {"mode": "only_me", "allowed_telegram_ids": "42"})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "only_me"
        assert body["allowed_telegram_ids"] == "42"
        assert body["rollout"] == "not_deployed"
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unchanged_set_reports_an_outstanding_rollout_as_pending(self):
        """'Nothing changed' must not mask an unpublished rollout from before."""
        project = _project(_audience_private())
        outstanding_run = MagicMock()
        outstanding_run.id = "botrollout-outstanding1"
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                # find_publish_owed_run finds the interrupted rollout.
                _run_query_result(outstanding_run),
            ]
        )

        resp = await self._post(session, {"mode": "only_me", "allowed_telegram_ids": "42"})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rollout"] == "pending"
        assert body["rollout_run_id"] == "botrollout-outstanding1"

    @pytest.mark.asyncio
    async def test_changing_the_audience_still_launches_the_rollout(self):
        project = _project(_audience_private())
        session = _ScriptedSession(
            [
                _locked_project_result(project),
                _no_target(),
                _running_without_sha_answer(absent=True),
            ]
        )
        redis = MagicMock()
        redis.publish_message = AsyncMock()
        _client(session, redis)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                with patch_recipient("111"):
                    resp = await c.post(
                        f"/api/projects/{PROJECT_UUID}/config/bot-access",
                        json={"mode": "custom", "allowed_telegram_ids": "42,84"},
                        headers={"X-Internal-Key": "test-internal-key"},
                    )
        finally:
            _drop_overrides()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "custom"
        assert body["allowed_telegram_ids"] == "42,84"
        # Nothing running: the write lands but there is nothing to roll out.
        assert body["rollout"] == "not_deployed"
        assert body["rollout_run_id"] is None
