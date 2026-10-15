"""The worker-create ack is validated once at the transport boundary."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.worker import AgentType, WorkerOwnership
from src.clients import worker_spawner
from src.clients.qa_worker import QAExecutorUnavailable, run_qa_executor
from src.clients.worker_spawner import WorkerCreateAckError, _wait_for_create_ack, request_spawn

_OWNERSHIP = WorkerOwnership(
    story_id="story-1", project_id="proj-1", run_id="run-1", attempt_id="eng-attempt-1"
)

INVALID_ACKS = {
    "missing success": {"request_id": "req-1", "worker_id": "w-1"},
    "wrong type": {"request_id": "req-1", "success": "perhaps", "worker_id": "w-1"},
    "request_id mismatch": {"request_id": "req-2", "success": True, "worker_id": "w-1"},
    "success without worker_id": {"request_id": "req-1", "success": True},
}


def _raw_ack(payload: dict | None):
    return patch.object(worker_spawner, "_wait_for_response", AsyncMock(return_value=payload))


async def test_a_valid_ack_is_returned_typed():
    with _raw_ack({"request_id": "req-1", "success": True, "worker_id": "w-1"}):
        ack = await _wait_for_create_ack(AsyncMock(), "g", "c", "req-1")

    assert (ack.success, ack.worker_id, ack.error) == (True, "w-1", None)


async def test_a_refusal_ack_is_returned_typed():
    with _raw_ack({"request_id": "req-1", "success": False, "error": "project locked"}):
        ack = await _wait_for_create_ack(AsyncMock(), "g", "c", "req-1")

    assert (ack.success, ack.worker_id, ack.error) == (False, None, "project locked")


async def test_no_ack_within_the_creation_timeout_is_none():
    with _raw_ack(None):
        assert await _wait_for_create_ack(AsyncMock(), "g", "c", "req-1") is None


@pytest.mark.parametrize("payload", INVALID_ACKS.values(), ids=INVALID_ACKS.keys())
async def test_an_invalid_ack_raises_the_protocol_error(payload):
    with _raw_ack(payload), pytest.raises(WorkerCreateAckError, match="req-"):
        await _wait_for_create_ack(AsyncMock(), "g", "c", "req-1")


@pytest.mark.parametrize("payload", INVALID_ACKS.values(), ids=INVALID_ACKS.keys())
async def test_the_spawner_turns_an_invalid_ack_into_a_failed_spawn(payload):
    redis_client = AsyncMock()
    settings = MagicMock(redis_url="redis://localhost:6379")
    with (
        patch.object(worker_spawner, "get_settings", return_value=settings),
        patch.object(worker_spawner.redis, "from_url", return_value=redis_client),
        patch("src.prompts.load_developer_instructions", return_value="instructions"),
        _raw_ack(payload),
    ):
        result = await request_spawn(repo="org/repo", task_content="t", ownership=_OWNERSHIP)

    assert result.success is False
    assert result.output.startswith("Creation failed: ")
    assert "request" in result.output
    assert result.worker_id is None
    published = [json.loads(call.args[1]["data"]) for call in redis_client.xadd.await_args_list]
    assert [command["command"] for command in published] == ["create"]


@pytest.mark.parametrize("payload", INVALID_ACKS.values(), ids=INVALID_ACKS.keys())
async def test_qa_cleans_up_and_fails_to_start_on_an_invalid_ack(payload):
    redis_client = AsyncMock()
    with (
        patch("src.clients.qa_worker.redis.from_url", return_value=redis_client),
        _raw_ack(payload),
        pytest.raises(QAExecutorUnavailable) as unavailable,
    ):
        await run_qa_executor(
            agent_type=AgentType.CLAUDE,
            ownership=_OWNERSHIP,
            deploy_target_url="http://203.0.113.10:8080",
            capability_url="http://qa-worker:41234/qa/call",
            capability_token="run-token",  # noqa: S106 - fake endpoint credential
            instructions="# QA executor",
            prompt="test it",
            verdict_received=asyncio.Event(),
            calls_served=lambda: 0,
            timeout=1,
            on_create_published=lambda: None,
        )

    assert unavailable.value.transient is False
    assert "create ack" in str(unavailable.value)
    published = [json.loads(call.args[1]["data"]) for call in redis_client.xadd.await_args_list]
    [create, delete] = published
    assert create["command"] == "create"
    assert (delete["command"], delete["worker_id"], delete["reason"]) == (
        "delete",
        create["config"]["name"],
        "completed",
    )
