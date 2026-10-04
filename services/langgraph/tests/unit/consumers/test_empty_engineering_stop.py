"""Empty results use the durable story stop before the attempt is settled."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.run import EMPTY_RESULT_TERMINAL_KEY
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
    # The strict outcome is durable, but no terminal status was written.
    assert all("status" not in call.kwargs["json"] for call in api.patch.await_args_list)
    retained = api.patch.await_args.kwargs["json"]["run_metadata"][EMPTY_RESULT_TERMINAL_KEY]
    assert retained["result"]["failure_reason"] == "no_new_commit"


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
    assert [call.args[0] for call in api.patch.await_args_list] == ["runs/eng-1", "runs/eng-1"]
    assert "status" not in api.patch.await_args_list[0].kwargs["json"]
    assert api.patch.await_args_list[1].kwargs["json"]["status"] == "failed"
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


@pytest.mark.asyncio
async def test_fenced_queue_reclaim_finishes_the_exact_retained_outcome_without_the_graph(
    monkeypatch,
):
    from shared.contracts.dto.run import EmptyEngineeringTerminal
    from src.consumers import engineering

    terminal = EmptyEngineeringTerminal.model_validate(
        {
            "status": "failed",
            "error_message": "No new commit",
            "result": {"engineering_status": "failed", "failure_reason": "no_new_commit"},
            "engineering_attempt": {"provider": "openai", "model": "fixture", "input_tokens": 17},
            "transcript_path": "/fixture/retained.jsonl",
        }
    ).model_dump(mode="json", exclude_unset=True)
    api = AsyncMock()
    api.get_run.return_value = SimpleNamespace(
        status="running",
        story_id="story-1",
        run_metadata={EMPTY_RESULT_TERMINAL_KEY: terminal},
    )
    monkeypatch.setattr(
        engineering, "_engineering_attempt_authority", AsyncMock(return_value="stopped")
    )
    with (
        patch.object(engineering, "api_client", api),
        patch("src.consumers.engineering_result_handler.api_client", api),
        patch(
            "src.consumers.engineering_result_handler._park_story_without_new_commit", AsyncMock()
        ) as stop,
        patch("src.subgraphs.engineering.create_engineering_subgraph") as graph,
    ):
        result = await engineering.process_engineering_job(
            {
                "task_id": "eng-retained",
                "project_id": str(make_project().id),
                "initiating_run_id": "init-fixture",
                "story_id": "story-1",
                "action": "fix",
                "description": "Settled output",
                "telegram_chat_id": "",
                "skip_deploy": False,
            },
            AsyncMock(),
        )
    assert result["status"] == "failed"
    graph.assert_not_called()
    stop.assert_awaited_once_with("story-1", "eng-retained", "No new commit")
    api.patch.assert_awaited_once_with("runs/eng-retained", json=terminal)


@pytest.mark.asyncio
async def test_failed_retention_delivery_never_falls_through_to_generic_failure():
    api = AsyncMock()
    api.patch.side_effect = ConnectionError("Retention delivery unavailable")
    with patch("src.consumers.engineering_result_handler.api_client", api):
        with pytest.raises(EmptyResultSettlementError, match="not retained"):
            await fail_job(
                "eng-empty",
                "No new commit",
                redis=AsyncMock(),
                turn_result_consumed=True,
                story_id="story-1",
                failure_reason=EngineeringFailureReason.NO_NEW_COMMIT,
            )
    api.stop_story.assert_not_awaited()
    assert "status" not in api.patch.await_args.kwargs["json"]
