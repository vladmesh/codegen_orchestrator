"""Service coverage for engineering-turn adoption across a consumer restart."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.contracts.worker_turn import AttemptTurnMetadata, WorkerActiveTurn, active_turn_key
from shared.queues import WORKER_COMMANDS


@pytest.mark.asyncio
async def test_adoption_reads_only_the_durable_turn_result_without_publishing_a_prompt(real_redis):
    """A replacement consumer settles the turn its predecessor was awaiting."""
    from src.clients.worker_spawner import await_turn_output

    worker_id = "adoption-worker"
    request_id = "turn-being-adopted"
    output_stream = f"worker:{worker_id}:output"
    input_stream = f"worker:{worker_id}:input"

    await real_redis.xadd(
        output_stream,
        {
            "request_id": request_id,
            "data": json.dumps(
                {
                    "status": "completed",
                    "content": "the existing worker finished",
                    "commit_sha": "a" * 40,
                }
            ),
        },
    )

    result = await await_turn_output(
        real_redis,
        worker_id=worker_id,
        request_id=request_id,
        timeout_seconds=1,
    )

    assert result is not None
    assert result.success is True
    assert result.request_id == request_id
    assert result.commit_sha == "a" * 40
    assert await real_redis.xlen(input_stream) == 0


@pytest.mark.asyncio
async def test_adoption_skips_historical_poison_before_its_matching_result(real_redis):
    """A bad retained result for another turn cannot strand the live turn."""
    from src.clients.worker_spawner import await_turn_output

    worker_id = "adoption-poison-worker"
    request_id = "turn-being-adopted"
    output_stream = f"worker:{worker_id}:output"

    await real_redis.xadd(
        output_stream,
        {"request_id": "completed-earlier-turn", "data": "not-json"},
    )
    await real_redis.xadd(
        output_stream,
        {
            "request_id": request_id,
            "data": json.dumps(
                {
                    "status": "completed",
                    "content": "the adopted turn finished",
                    "commit_sha": "b" * 40,
                }
            ),
        },
    )

    result = await await_turn_output(
        real_redis,
        worker_id=worker_id,
        request_id=request_id,
        timeout_seconds=1,
    )

    assert result is not None
    assert result.success is True
    assert result.request_id == request_id
    assert result.commit_sha == "b" * 40


@pytest.mark.asyncio
async def test_terminal_failure_tears_down_an_unconsumed_recorded_turn(real_redis):
    """A terminal failure cannot leave its recorded worker executing without a waiter."""
    from datetime import UTC, datetime, timedelta

    from src.consumers.engineering_result_handler import prepare_terminal_settlement

    task_id = "eng-terminal-settlement"
    worker_id = "terminal-settlement-worker"
    request_id = "terminal-settlement-turn"
    active_key = active_turn_key(worker_id)
    await real_redis.hset(
        active_key,
        mapping=WorkerActiveTurn(
            worker_id=worker_id,
            attempt_id=task_id,
            request_id=request_id,
            lease_id="lease-1",
            started_at=datetime.now(UTC),
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        ).as_redis_fields(),
    )
    redis = SimpleNamespace(redis=real_redis)
    run = SimpleNamespace(
        run_metadata=AttemptTurnMetadata(
            worker_id=worker_id,
            active_turn_request_id=request_id,
        ).as_run_metadata()
    )

    try:
        with patch(
            "src.consumers.engineering_result_handler.api_client.get_run",
            new=AsyncMock(return_value=run),
        ):
            await prepare_terminal_settlement(task_id, redis=redis, turn_result_consumed=False)

        messages = await real_redis.xrange(WORKER_COMMANDS)
        assert len(messages) == 1
        command = DeleteWorkerCommand.model_validate_json(messages[0][1][b"data"])
        assert command.worker_id == worker_id
        assert command.reason == "failed"
    finally:
        await real_redis.delete(active_key)


@pytest.mark.asyncio
async def test_terminal_failure_does_not_delete_a_worker_leased_by_another_attempt(real_redis):
    """An owner-fenced lease takes precedence over the failed attempt's teardown."""
    from datetime import UTC, datetime, timedelta

    from src.consumers.engineering_result_handler import prepare_terminal_settlement

    task_id = "eng-terminal-settlement"
    worker_id = "other-attempt-worker"
    active_key = active_turn_key(worker_id)
    await real_redis.hset(
        active_key,
        mapping=WorkerActiveTurn(
            worker_id=worker_id,
            attempt_id="eng-live-owner",
            request_id="live-owner-turn",
            lease_id="lease-2",
            started_at=datetime.now(UTC),
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        ).as_redis_fields(),
    )
    redis = SimpleNamespace(redis=real_redis)
    run = SimpleNamespace(
        run_metadata=AttemptTurnMetadata(
            worker_id=worker_id,
            active_turn_request_id="failed-attempt-turn",
        ).as_run_metadata()
    )

    try:
        with patch(
            "src.consumers.engineering_result_handler.api_client.get_run",
            new=AsyncMock(return_value=run),
        ):
            await prepare_terminal_settlement(task_id, redis=redis, turn_result_consumed=False)

        assert await real_redis.xlen(WORKER_COMMANDS) == 0
    finally:
        await real_redis.delete(active_key)


@pytest.mark.asyncio
async def test_consumed_turn_result_keeps_the_story_worker_available_for_reuse(real_redis):
    """Typed worker output settles the turn without requesting worker teardown."""
    from src.consumers.engineering_result_handler import prepare_terminal_settlement

    task_id = "eng-consumed-turn"
    worker_id = "reusable-story-worker"
    redis = SimpleNamespace(redis=real_redis)
    run = SimpleNamespace(
        run_metadata=AttemptTurnMetadata(
            worker_id=worker_id,
            active_turn_request_id="consumed-turn",
        ).as_run_metadata()
    )

    with patch(
        "src.consumers.engineering_result_handler.api_client.get_run",
        new=AsyncMock(return_value=run),
    ) as get_run:
        await prepare_terminal_settlement(task_id, redis=redis, turn_result_consumed=True)

    get_run.assert_not_awaited()
    assert await real_redis.xlen(WORKER_COMMANDS) == 0
