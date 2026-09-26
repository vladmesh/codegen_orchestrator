"""QA reports executor starts and typed cost evidence at its terminal boundary."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput
from shared.contracts.dto.run_result import QABlocker, QABlockerCategory
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from src.clients.qa_worker import _output_of, run_qa_executor
from src.consumers._qa_runner import QAExecutorAttempts, QAResult
from src.consumers.qa import _handle_qa_blocked, _handle_qa_fail, _handle_qa_pass


@pytest.mark.parametrize(
    ("payload", "cost"),
    [
        ({"claude_evidence": {"cost_microusd": 125}}, 125),
        ({"status": "failed", "error": "codex ended"}, None),
        ({"claude_evidence": {"cost_microusd": -1}}, None),
    ],
)
@pytest.mark.asyncio
async def test_qa_output_parses_provider_facts_before_transcript_truncation(payload, cost):
    task = asyncio.get_running_loop().create_future()
    task.set_result({**payload, "large": "x" * 22000})
    transcript, attempt = _output_of(task)
    assert len(transcript) == 20000
    assert (attempt.cost_microusd if attempt else None) == cost


@pytest.mark.asyncio
async def test_qa_output_keeps_the_content_not_a_deleted_transcript_locator():
    task = asyncio.get_running_loop().create_future()
    task.set_result(
        {
            "status": "failed",
            "transcript_path": "/artifacts/worker-transcripts/qa-deleted.log",
            "transcript_truncated": False,
            "error": "executor stopped",
        }
    )

    transcript, _ = _output_of(task)

    assert "transcript_path" not in transcript
    assert "executor stopped" in transcript


def _claude_fact(cost: int, tokens: int) -> EngineeringAttemptLedgerInput:
    return EngineeringAttemptLedgerInput.model_validate(
        {
            "claude_evidence": {
                "model": "claude-test",
                "cost_microusd": cost,
                "input_tokens": tokens,
                "output_tokens": tokens // 2,
                "cache_read_tokens": tokens // 4,
                "cache_write_tokens": tokens // 8,
            }
        }
    )


def test_qa_attempts_sum_every_started_executor_fact():
    attempts = QAExecutorAttempts(2)
    attempts.record_start(1)
    attempts.with_attempt(1, "first", _claude_fact(700_000, 80))
    attempts.record_start(2)
    attempts.with_attempt(2, "second", _claude_fact(300_000, 40))

    fact = attempts.accounting
    assert fact.executor_started is True
    assert fact.attempt.cost_microusd == 1_000_000
    assert fact.attempt.cost_source == "provider_reported"
    assert fact.attempt.provider == "anthropic"
    assert fact.attempt.model == "claude-test"
    assert fact.attempt.input_tokens == 120
    assert fact.attempt.output_tokens == 60
    assert fact.attempt.total_tokens == 180
    assert fact.attempt.cache_read_tokens == 30
    assert fact.attempt.cache_write_tokens == 15


def test_qa_attempt_without_facts_makes_whole_run_unknown():
    attempts = QAExecutorAttempts(2)
    attempts.record_start(1)
    attempts.with_attempt(1, "first", _claude_fact(700_000, 80))
    attempts.record_start(2)
    attempts.with_attempt(2, "", None)

    fact = attempts.accounting
    assert fact.executor_started is True
    assert fact.attempt.cost_source == "unknown"
    assert fact.attempt.cost_microusd is None


def test_qa_create_without_result_is_started_and_health_only_is_not():
    attempts = QAExecutorAttempts(2)
    assert attempts.accounting.executor_started is False
    attempts.record_start(1)
    assert attempts.accounting.executor_started is True
    assert attempts.accounting.attempt.cost_source == "unknown"


@pytest.mark.asyncio
async def test_published_create_records_start_before_a_later_redis_error():
    attempts = QAExecutorAttempts(2)
    events = []
    redis_client = AsyncMock()

    async def publish(*args, **kwargs):
        events.append("published" if not events else "cleanup")

    def record_start():
        events.append("recorded")
        attempts.record_start(1)

    redis_client.xadd.side_effect = publish
    with (
        patch("src.clients.qa_worker.redis.from_url", return_value=redis_client),
        patch(
            "src.clients.qa_worker._wait_for_response",
            new_callable=AsyncMock,
            side_effect=RuntimeError("redis failed after create"),
        ),
    ):
        with pytest.raises(RuntimeError, match="redis failed after create"):
            await run_qa_executor(
                agent_type=AgentType.CLAUDE,
                ownership=WorkerOwnership(
                    story_id="story-1",
                    project_id="project-1",
                    run_id="qa-1",
                    attempt_id="qa-1",
                ),
                deploy_target_url="http://203.0.113.10:8080",
                capability_url="http://127.0.0.1:8000",
                capability_token="test-token",  # noqa: S106 - fake endpoint credential
                instructions="test",
                prompt="test",
                verdict_received=asyncio.Event(),
                calls_served=lambda: 0,
                timeout=1,
                on_create_published=record_start,
            )

    assert events[:2] == ["published", "recorded"]
    assert attempts.accounting.executor_started is True
    assert attempts.accounting.attempt.cost_source == "unknown"
    redis_client.delete.assert_awaited_once()
    assert redis_client.delete.await_args.args[0].startswith("worker:qa-")
    assert redis_client.delete.await_args.args[0].endswith(":output")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["passed", "failed", "exhausted", "blocked", "health-only"])
async def test_terminal_qa_writers_send_explicit_executor_fact(outcome):
    blocker = QABlocker(
        category=QABlockerCategory.UNKNOWN,
        attempted="verify",
        sent="request",
        received="unavailable",
    )
    attempt = EngineeringAttemptLedgerInput.model_validate(
        {"claude_evidence": {"cost_microusd": 125}}
    )
    transcript = None if outcome == "health-only" else "executor output"
    attempts = QAExecutorAttempts(2)
    if transcript is not None:
        attempts.record_start(1)
        attempts.with_attempt(1, transcript, attempt)
    with patch("src.consumers.qa.api_client.patch", new_callable=AsyncMock) as api_patch:
        if outcome in {"passed", "health-only"}:
            await _handle_qa_pass(
                run_id="qa-1",
                project_id="proj-1",
                attempts=attempts,
                deployed_url="http://example.test",
                executor_transcript=transcript,
                executor_attempt=attempt if transcript is not None else None,
            )
        elif outcome == "blocked":
            await _handle_qa_blocked(
                run_id="qa-1",
                attempts=attempts,
                blocker=blocker,
                executor_transcript=transcript,
                executor_attempt=attempt,
            )
        else:
            await _handle_qa_fail(
                run_id="qa-1",
                project_id="project-1",
                attempts=attempts,
                qa_attempt=2 if outcome == "exhausted" else 1,
                qa_result=QAResult(
                    passed=False,
                    checks=[{"name": "check", "pass": False, "detail": "failed"}],
                    executor_evidence=transcript,
                    executor_attempt=attempt,
                ),
            )
    accounting = api_patch.call_args.kwargs["json"]["qa_accounting"]
    assert accounting["executor_started"] is (transcript is not None)
    if transcript is None:
        assert accounting["attempt"] is None
    else:
        assert accounting["attempt"]["cost_microusd"] == 125
