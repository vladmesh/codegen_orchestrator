"""Service coverage for engineering-turn adoption across a consumer restart."""

from __future__ import annotations

import json

import pytest


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
