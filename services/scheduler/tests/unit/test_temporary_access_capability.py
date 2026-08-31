"""Capability-backed temporary QA access is durable before dispatch."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.temporary_access import TemporaryAccessGrantDTO, TemporaryAccessStatus
from shared.contracts.queues.qa import QAMessage
from shared.queues import DEPLOY_QUEUE

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def _message() -> QAMessage:
    return QAMessage(
        project_id=PROJECT_ID,
        initiating_run_id="live-1",
        telegram_chat_id="",
        deployed_url="https://exact.example.com",
        application_id=42,
        acceptance_criteria="the bot answers /start",
        run_id="qa-1",
    )


def _stored(request) -> TemporaryAccessGrantDTO:
    return TemporaryAccessGrantDTO(
        **request.model_dump(mode="json"),
        status=TemporaryAccessStatus.GRANTING,
        granted_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_grant_persists_the_verified_identity_and_exact_target_before_dispatch() -> None:
    from src.tasks.temporary_access import grant_temporary_access

    api = AsyncMock()
    api.create_temporary_access_grant.side_effect = _stored
    api.get_run_if_missing_returns_none.return_value = None
    redis = AsyncMock()

    grant = await grant_temporary_access(
        api,
        redis,
        project_id=PROJECT_ID,
        target_application_id=42,
        target_base_url="https://exact.example.com",
        head_sha="a" * 40,
        qa_message=_message(),
    )

    request = api.create_temporary_access_grant.await_args.args[0]
    assert request.channel == "telegram"
    assert request.external_id == "8202532144"
    assert request.target_application_id == 42
    assert request.target_base_url == "https://exact.example.com"
    assert {"capability", "bot_token", "env_key"}.isdisjoint(request.model_dump())
    assert grant is not None
    published = redis.publish_message.await_args
    assert published.args[0] == DEPLOY_QUEUE
    assert published.args[1].env_overrides == {}
    assert api.create_temporary_access_grant.await_count == 1
