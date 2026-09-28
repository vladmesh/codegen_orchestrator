"""Empty results use the durable story stop before the attempt is settled."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.run_result import EngineeringFailureReason
from shared.contracts.vocab import AgentType
from src.clients.worker_spawner import SpawnResult
from src.consumers.engineering_result_handler import (
    EmptyResultSettlementError,
    EngineeringSuccessParams,
    fail_job,
    handle_engineering_success,
)
from src.nodes.developer import DeveloperNode
from tests.unit.factories import make_project


def test_worker_success_without_a_commit_has_the_no_new_commit_classification():
    result = DeveloperNode._build_result_state(
        SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Authorization: Bearer canary",
            turn_result_consumed=True,
        ),
        "product",
        "org/product",
        {"executor_decision": SimpleNamespace(agent_type=AgentType.CLAUDE)},
    )
    assert result["failure_reason"] is EngineeringFailureReason.NO_NEW_COMMIT
    assert result["turn_result_consumed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_sha", [None, ""])
async def test_defensive_success_without_a_commit_uses_the_same_durable_stop(commit_sha):
    api = AsyncMock()
    redis = AsyncMock()
    with patch("src.consumers.engineering_result_handler.api_client", api):
        outcome = await handle_engineering_success(
            EngineeringSuccessParams(
                result={"commit_sha": commit_sha},
                task_id="eng-1",
                project=make_project(),
                callback_stream=None,
                redis=redis,
                skip_deploy=False,
                story_id="story-1",
            )
        )
    assert outcome["status"] == "failed"
    assert api.patch.await_args.kwargs["json"]["result"]["failure_reason"] == "no_new_commit"
    api.stop_story.assert_awaited_once()
    failure = api.stop_story.await_args.args[2]
    assert failure.code.value == "no_new_commit"
    assert "no commit" in failure.detail.lower()
    api.transition_story.assert_not_awaited()
    redis.publish_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_refusal_leaves_the_run_nonterminal_for_reclaim():
    api = AsyncMock()
    api.stop_story.side_effect = RuntimeError("stop refused")
    with patch("src.consumers.engineering_result_handler.api_client", api):
        with pytest.raises(RuntimeError, match="story.*stop"):
            await fail_job(
                "eng-1",
                "Worker made no new commit",
                redis=AsyncMock(),
                turn_result_consumed=True,
                story_id="story-1",
                failure_reason=EngineeringFailureReason.NO_NEW_COMMIT,
            )
    api.patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_does_not_depend_on_reason_patch_or_redis_publication():
    api = AsyncMock()
    redis = AsyncMock()
    redis.publish_flat.side_effect = RuntimeError("Redis unavailable")
    with patch("src.consumers.engineering_result_handler.api_client", api):
        outcome = await fail_job(
            "eng-1",
            "Worker made no new commit",
            redis=redis,
            turn_result_consumed=True,
            story_id="story-1",
            failure_reason=EngineeringFailureReason.NO_NEW_COMMIT,
            telegram_chat_id="777",
        )
    assert outcome["status"] == "failed"
    api.stop_story.assert_awaited_once()
    assert [call.args[0] for call in api.patch.await_args_list] == ["runs/eng-1"]
    api.transition_story.assert_not_awaited()
    redis.publish_flat.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_outcome_survives_a_required_worker_settlement_failure():
    api = AsyncMock()
    with (
        patch("src.consumers.engineering_result_handler.api_client", api),
        patch(
            "src.consumers.engineering_result_handler.prepare_terminal_settlement",
            AsyncMock(side_effect=ConnectionError("teardown unavailable")),
        ),
    ):
        with pytest.raises(EmptyResultSettlementError):
            await fail_job(
                "eng-1",
                "Worker made no new commit",
                redis=AsyncMock(),
                failure_reason=EngineeringFailureReason.NO_NEW_COMMIT,
            )
    api.patch.assert_not_awaited()
