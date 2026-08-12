"""Who hears about work the API dispatches, and what happens when nobody can.

``Project.owner_id`` is an internal ``User.id``; Telegram addresses
``User.telegram_id``. The API resolves one into the other before it publishes. An
owner that cannot be resolved is an admin alert naming the story, the project and
the event — the three identifiers somebody needs to find the work — never a
silently empty recipient.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from src.database import get_async_session
from src.dependencies import get_redis_client
from src.main import app
from src.routers._recipients import (
    UNRESOLVED_REASON,
    ProjectRecipient,
    resolve_project_chat_id,
    resolve_project_recipient,
)
from src.routers.applications import ADMIN_ACTION_UNADDRESSED_REASON, stage_undeploy

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
OWNER_TELEGRAM_ID = 987654321


def _session(telegram_id):
    """A session whose one query answers with the owner's telegram id (or None)."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=telegram_id)
    session.execute = AsyncMock(return_value=result)
    return session


class TestResolveProjectChatId:
    @pytest.mark.asyncio
    async def test_the_owners_telegram_id_is_the_destination(self):
        with patch("src.routers._recipients.notify_admins_best_effort", new=AsyncMock()) as alert:
            chat_id = await resolve_project_chat_id(
                _session(OWNER_TELEGRAM_ID), PROJECT_ID, event="story_sent_to_architect"
            )

        assert chat_id == str(OWNER_TELEGRAM_ID)
        alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unresolvable_owner_alerts_admins_with_story_project_and_event(self):
        with patch("src.routers._recipients.notify_admins_best_effort", new=AsyncMock()) as alert:
            chat_id = await resolve_project_chat_id(
                _session(None),
                PROJECT_ID,
                event="story_sent_to_architect",
                story_id="story-7",
            )

        assert chat_id == ""
        alert.assert_awaited_once()
        text = alert.await_args.args[0]
        assert "story-7" in text
        assert str(PROJECT_ID) in text
        assert "story_sent_to_architect" in text
        assert alert.await_args.kwargs["story_id"] == "story-7"
        assert alert.await_args.kwargs["level"] == "error"

    @pytest.mark.asyncio
    async def test_the_recipient_carries_the_reason_when_it_cannot_be_resolved(self):
        with patch("src.routers._recipients.notify_admins_best_effort", new=AsyncMock()):
            recipient = await resolve_project_recipient(
                _session(None), PROJECT_ID, event="project_teardown"
            )

        assert recipient.telegram_chat_id == ""
        assert recipient.unaddressed_reason == UNRESOLVED_REASON


class TestStageUndeploy:
    def test_an_owner_requested_teardown_is_addressed_to_the_owner(self):
        application = MagicMock(id=7, status="running")
        db = MagicMock()

        _run, msg = stage_undeploy(
            application,
            PROJECT_ID,
            db,
            triggered_by=DeployTrigger.PO,
            recipient=ProjectRecipient(telegram_chat_id=str(OWNER_TELEGRAM_ID)),
        )

        assert msg.telegram_chat_id == str(OWNER_TELEGRAM_ID)
        assert msg.unaddressed_reason == ""

    def test_an_admin_teardown_says_why_it_reports_to_nobody(self):
        application = MagicMock(id=7, status="running")
        db = MagicMock()

        _run, msg = stage_undeploy(
            application,
            PROJECT_ID,
            db,
            triggered_by=DeployTrigger.ADMIN,
            recipient=ProjectRecipient(unaddressed_reason=ADMIN_ACTION_UNADDRESSED_REASON),
        )

        assert msg.telegram_chat_id == ""
        assert msg.unaddressed_reason == ADMIN_ACTION_UNADDRESSED_REASON


def _application(**overrides):
    now = datetime.now(UTC)
    defaults = {
        "id": 1,
        "repo_id": "repo-1",
        "server_handle": "vps-1",
        "service_name": "test-bot",
        "status": "running",
        "last_health_check": None,
        "created_at": now,
        "updated_at": now,
        "port_allocations": [],
    }
    defaults.update(overrides)
    obj = MagicMock()
    for key, value in defaults.items():
        setattr(obj, key, value)
    return obj


def _stop_session(application):
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=application)
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=MagicMock(project_id=PROJECT_ID))
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        return None

    session.refresh = _refresh
    return session


class TestAdminLifecycleEndpoints:
    @pytest.mark.asyncio
    async def test_stop_publishes_a_deploy_that_states_it_has_no_user_recipient(self):
        """An operator's stop is not a user's story event, and says so explicitly."""
        application = _application()
        redis = MagicMock()
        redis.publish_message = AsyncMock()
        app.dependency_overrides[get_async_session] = lambda: _stop_session(application)
        app.dependency_overrides[get_redis_client] = lambda: redis
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/applications/1/stop", headers=INTERNAL_HEADERS)
        finally:
            app.dependency_overrides.pop(get_async_session, None)
            app.dependency_overrides.pop(get_redis_client, None)

        assert resp.status_code == 200, resp.text
        redis.publish_message.assert_awaited_once()
        msg = redis.publish_message.await_args.args[1]
        assert isinstance(msg, DeployMessage)
        assert msg.action == DeployAction.STOP
        assert msg.telegram_chat_id == ""
        assert msg.unaddressed_reason == ADMIN_ACTION_UNADDRESSED_REASON
