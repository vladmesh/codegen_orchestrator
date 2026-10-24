"""The deploy-to-QA handoff carries the story's confirmed order beside the repository's criteria.

The brief is the shared INSTALL-only record (`shared/tests/confirmed_order_cases.py`):
its plan left the repository at the scaffold's health check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from _run_routing_factories import _make_project, _make_repo, _make_run, _make_story
import httpx
import pytest

from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.dto.user import UserDTO
from shared.contracts.dto.work_admission import (
    PaidRunStartRead,
    WorkAdmissionOutcome,
    WorkAdmissionRead,
)
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAConfirmedOrder
from shared.queues import QA_QUEUE
from shared.tests.confirmed_order_cases import SCAFFOLD_CRITERIA, channels_brief


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_stories_by_status.return_value = [_make_story(status="deploying")]
    client.get_latest_run_by_story.return_value = _make_run(
        result={
            "deploy_outcome": DeployOutcome.SUCCESS.value,
            "deployed_url": "https://channels.example.com",
            "application_id": 42,
        },
    )
    client.get_primary_repository.return_value = _make_repo(acceptance_criteria=SCAFFOLD_CRITERIA)
    client.get_project.return_value = _make_project()
    client.get_user.return_value = UserDTO(
        id=1, telegram_id=900000001, created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    client.start_paid_run.return_value = PaidRunStartRead(
        admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED), run_id="qa-test"
    )
    return client


async def _handoff(api_client) -> dict:
    from src.tasks.supervisor import supervise_deploying_stories

    redis_client = AsyncMock()
    counts = await supervise_deploying_stories(api_client, redis_client)
    published = [
        call.args[1]
        for call in redis_client.publish_message.call_args_list
        if call.args[0] == QA_QUEUE
    ]
    return {"counts": counts, "published": published}


async def test_a_confirmed_brief_is_the_order_on_the_stored_plan_and_the_message(api_client):
    api_client.get_product_brief_by_story.return_value = channels_brief()

    handed = await _handoff(api_client)

    api_client.get_product_brief_by_story.assert_awaited_once_with("story-1")
    command = api_client.start_paid_run.await_args.args[0]
    plan = QAHandoffPlan.model_validate(command.run_metadata[QA_HANDOFF_KEY])
    order = plan.qa_message.confirmed_order
    assert order == QAConfirmedOrder.from_brief(channels_brief())
    assert (order.brief_id, order.revision, order.language) == ("brief-channels", 2, "ru")
    assert [example.user_sends for example in order.usage_examples] == [
        "/channels",
        "/addchannel https://t.me/example",
        "/removechannel https://t.me/example",
        "/digest",
    ]
    assert plan.qa_message.acceptance_criteria == SCAFFOLD_CRITERIA
    assert handed["published"] == [plan.qa_message]
    assert handed["counts"]["tested"] == 1


async def test_an_unconfirmed_brief_orders_nothing(api_client):
    api_client.get_product_brief_by_story.return_value = channels_brief(confirmed=False)

    handed = await _handoff(api_client)

    [message] = handed["published"]
    assert message.confirmed_order is None
    assert message.acceptance_criteria == SCAFFOLD_CRITERIA


async def test_a_failed_brief_read_never_hands_scaffold_criteria_to_qa(api_client):
    request = httpx.Request("GET", "http://api/product-briefs/by-story/story-1")
    api_client.get_product_brief_by_story.side_effect = httpx.HTTPStatusError(
        "unavailable", request=request, response=httpx.Response(503, request=request)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await _handoff(api_client)

    api_client.start_paid_run.assert_not_awaited()
    api_client.transition_story.assert_not_awaited()
    api_client.fail_story.assert_not_awaited()
