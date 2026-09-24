"""QA reports executor starts and typed cost evidence at its terminal boundary."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput
from shared.contracts.dto.run_result import QABlocker, QABlockerCategory
from src.clients.qa_worker import _output_of
from src.consumers._qa_runner import QAResult
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
    with patch("src.consumers.qa.api_client.patch", new_callable=AsyncMock) as api_patch:
        if outcome in {"passed", "health-only"}:
            await _handle_qa_pass(
                run_id="qa-1",
                deployed_url="http://example.test",
                executor_transcript=transcript,
                executor_attempt=attempt if transcript is not None else None,
            )
        elif outcome == "blocked":
            await _handle_qa_blocked(
                run_id="qa-1",
                blocker=blocker,
                executor_transcript=transcript,
                executor_attempt=attempt,
            )
        else:
            await _handle_qa_fail(
                run_id="qa-1",
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
