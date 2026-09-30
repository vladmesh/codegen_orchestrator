"""A real Claude result document reaches the ledger as a provider-reported cost.

The document is the one captured from the pinned Claude Code CLI and committed with
worker-wrapper's tests. It is parsed by the wrapper's own evidence reader, crosses the
worker-result wire, and is then read by the developer and QA paths exactly as a live
turn's would be.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from worker_wrapper.observability import _extract_claude_evidence

from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput
from shared.contracts.queues.worker_result import WorkerCompletedResult, WorkerFailedResult
from shared.contracts.vocab import AgentType
from src.clients import qa_worker
from src.clients.worker_spawner import spawn_result_from_output
from src.consumers._qa_runner import QAExecutorAttempts
from src.consumers.engineering_result_handler import _observability_patch
from src.nodes.developer import DeveloperNode

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "worker-wrapper"
    / "tests"
    / "fixtures"
    / "claude_code_2.1.278_result.json"
)


def _evidence():
    return _extract_claude_evidence(FIXTURE.read_text(encoding="utf-8"))["claude_evidence"]


def _developer_attempt(published) -> EngineeringAttemptLedgerInput:
    """What the developer node reads off the wire, down to the ledger's input."""
    spawned = spawn_result_from_output(
        published.model_dump(mode="json"), request_id="req-1", worker_id="dev-1"
    )
    observability = DeveloperNode._worker_observability(spawned, {"config": {}}, AgentType.CLAUDE)
    return EngineeringAttemptLedgerInput.model_validate(
        _observability_patch(observability)["engineering_attempt"]
    )


def test_a_developer_turn_with_the_document_is_a_provider_reported_attempt():
    """AC1: the result the wrapper publishes after the grace settles a known cost."""
    published = WorkerCompletedResult(
        commit_sha="a" * 40, content="did the task", claude_evidence=_evidence()
    )

    attempt = _developer_attempt(published)

    assert attempt.cost_source == "provider_reported"
    assert attempt.cost_microusd == 56_448
    assert attempt.provider == "anthropic"
    assert attempt.model == "claude-sonnet-5"
    assert (attempt.input_tokens, attempt.output_tokens, attempt.total_tokens) == (6, 340, 346)
    assert (attempt.cache_read_tokens, attempt.cache_write_tokens) == (73_740, 9_572)


def test_a_developer_turn_stopped_without_the_document_stays_unknown():
    """AC3: a CLI stopped past the grace prints nothing, and nothing is estimated."""
    published = WorkerCompletedResult(commit_sha="a" * 40, content="did the task")

    attempt = _developer_attempt(published)

    assert attempt.cost_source == "unknown"
    assert attempt.cost_microusd is None


def _qa_output(payload: dict | None, *, after: float):
    async def output(*_args, **_kwargs):
        await asyncio.sleep(after)
        return payload

    return output


async def _qa_attempt(payload: dict | None, *, output_after: float):
    verdict = asyncio.Event()
    verdict.set()
    with patch.object(qa_worker, "_wait_for_response", _qa_output(payload, after=output_after)):
        _, attempt = await qa_worker._await_verdict_or_exit(
            redis_client=None,
            group_name="qa",
            consumer_id="qa-1",
            output_stream="worker:qa-1:output",
            worker_id="qa-1",
            verdict_received=verdict,
            timeout=30,
        )
    return QAExecutorAttempts(1).with_attempt(1, "explored", attempt).accounting.attempt


@pytest.mark.asyncio
async def test_a_qa_executor_that_ends_after_its_verdict_records_a_provider_cost():
    """AC5: the QA CLI is never cut off by its verdict, so its document reaches the ledger."""
    payload = WorkerFailedResult(
        error="Agent exited without reporting result", claude_evidence=_evidence()
    ).model_dump(mode="json")

    attempt = await _qa_attempt(payload, output_after=0.05)

    assert attempt.cost_source == "provider_reported"
    assert attempt.cost_microusd == 56_448
    assert attempt.provider == "anthropic"


@pytest.mark.asyncio
async def test_a_qa_executor_that_outlives_the_verdict_grace_stays_unknown():
    """I3: output that arrives after the bounded wait is not read, so cost stays unknown."""
    payload = WorkerFailedResult(
        error="Agent exited without reporting result", claude_evidence=_evidence()
    ).model_dump(mode="json")

    with patch.object(qa_worker, "VERDICT_GRACE_S", 0.05):
        attempt = await _qa_attempt(payload, output_after=1)

    assert attempt.cost_source == "unknown"
    assert attempt.cost_microusd is None
