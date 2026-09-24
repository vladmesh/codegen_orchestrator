"""A QA check the transport cannot deliver does not fail a passing product.

Regression, sprint:1445 on 2026-09-17: QA passed every acceptance criterion of
a Telegram bot, also tried the checklist's "Empty input" edge case,
`telegram_probe` refused it as `not_applicable`, and the executor still reported
it as a failed `qa_capability` check. The run failed, the supervisor typed it
`qa_checks_unverifiable`, and the working bot was stopped. No harness runs the
LLM executor deterministically, so the executor here is scripted to submit the
attempt-three verdict, and the verdict goes through the real parse path the
runner uses: the run's workspace holds the verdict and the runtime's evidence.
"""

from __future__ import annotations

import json

from shared.contracts.dto.run_result import QAFailedCheck, QAOutcome, QARunResult
from src.agents.qa.tools import _TelegramCapability
from src.consumers._qa_runner import _verdict_of, parse_qa_result
from src.consumers._qa_workspace import qa_workspace
from src.prompts.qa import build_qa_prompt

CRITERIA = (
    "- /start отвечает приветствием\n"
    "- после дохода 5000 и расхода 300 /balance отвечает 4700\n"
    "- /history перечисляет записи"
)

_CRITERION_CHECKS = [
    {"name": name, "pass": True, "detail": f"sent {name}; reply matched the criterion"}
    for name in ("/start greeting", "/balance after income and expense", "/history lists records")
]

_NOT_APPLICABLE_EMPTY_INPUT = {
    "name": "Empty input",
    "not_applicable": True,
    "detail": "telegram_probe answered not_applicable: Telegram rejects an empty message",
}

_FAILED_EMPTY_INPUT = {
    "name": "Empty input",
    "pass": False,
    "detail": "telegram_probe answered not_applicable: Telegram rejects an empty message",
    "cause": "qa_capability",
}


class _Service:
    calls_served = 12


class _Said:
    evidence = "scripted executor"
    attempt = None


async def _never_runs(*_args, **_kwargs):
    raise AssertionError("an empty message must not reach the transport")


async def _judge(tmp_path, checks: list[dict], *, probe_empty: bool):
    """Run the scripted executor inside a real workspace and parse its verdict."""
    with qa_workspace(str(tmp_path)) as workspace:
        if probe_empty:
            telegram = _TelegramCapability(
                bot_username="financebot",
                workspace=workspace,
                telethon_env={},
                probe_runner=_never_runs,
            )
            answer = await telegram.telegram_probe("")
            assert "not_applicable" in answer
        passed = all(check.get("pass", True) for check in checks)
        workspace.submit_verdict(
            json.dumps({"pass": passed, "checks": checks, "summary": "attempt three"})
        )
        result = _verdict_of(workspace, _Service(), 600, _Said())
        result.telegram_probe_evidence = list(workspace.telegram_probe_evidence)
        return result


def _failed_checks(result) -> list[QAFailedCheck]:
    """The failed checks the QA consumer would persist on the run."""
    return [
        QAFailedCheck.model_validate(
            {"name": c["name"], "detail": c["detail"]}
            | ({"cause": c["cause"]} if "cause" in c else {})
        )
        for c in result.checks
        if not c.get("pass", True)
    ]


async def test_attempt_three_with_the_recorded_refusal_passes(tmp_path):
    result = await _judge(
        tmp_path, [*_CRITERION_CHECKS, _NOT_APPLICABLE_EMPTY_INPUT], probe_empty=True
    )

    assert result.passed is True
    assert result.blocker is None
    assert _failed_checks(result) == []
    [not_applicable] = [check for check in result.checks if check.get("not_applicable")]
    assert not_applicable["transport_refusal"] == "send '' to @financebot"
    # What the consumer persists for this verdict is an ordinary passed run.
    stored = QARunResult(
        qa_outcome=QAOutcome.PASSED,
        telegram_probe_evidence=result.telegram_probe_evidence,
    )
    assert stored.failed_checks == []
    assert stored.blocker is None


async def test_a_not_applicable_check_without_a_recorded_refusal_fails_as_qa_capability(tmp_path):
    result = await _judge(
        tmp_path, [*_CRITERION_CHECKS, _NOT_APPLICABLE_EMPTY_INPUT], probe_empty=False
    )

    assert result.passed is False
    assert result.blocker is None
    [failed] = _failed_checks(result)
    assert failed.name == "Empty input"
    assert failed.cause.value == "qa_capability"


async def test_each_not_applicable_check_needs_its_own_refusal(tmp_path):
    second = {**_NOT_APPLICABLE_EMPTY_INPUT, "name": "Whitespace input"}
    result = await _judge(
        tmp_path,
        [*_CRITERION_CHECKS, _NOT_APPLICABLE_EMPTY_INPUT, second],
        probe_empty=True,
    )

    assert result.passed is False
    [failed] = _failed_checks(result)
    assert failed.name == "Whitespace input"
    assert failed.cause.value == "qa_capability"


async def test_the_attempt_three_failed_shape_is_left_as_a_qa_capability_failure(tmp_path):
    """A failed check is not re-read as not applicable from its wording.

    The runner cannot tell this check from a criterion that needs a photo upload
    without trusting the executor's text, so it keeps the executor's own failure.
    """
    result = await _judge(tmp_path, [*_CRITERION_CHECKS, _FAILED_EMPTY_INPUT], probe_empty=True)

    assert result.passed is False
    [failed] = _failed_checks(result)
    assert failed.cause.value == "qa_capability"


async def test_a_product_failure_beside_a_not_applicable_check_still_fails(tmp_path):
    product_failure = {
        "name": "/balance after income and expense",
        "pass": False,
        "detail": "expected: /balance отвечает 4700; received: 5000",
        "cause": "product",
    }
    result = await _judge(
        tmp_path,
        [_CRITERION_CHECKS[0], product_failure, _NOT_APPLICABLE_EMPTY_INPUT],
        probe_empty=True,
    )

    assert result.passed is False
    assert result.blocker is None
    [failed] = _failed_checks(result)
    assert failed.name == "/balance after income and expense"
    assert failed.cause.value == "product"


def test_a_not_applicable_check_with_a_pass_or_cause_is_an_invalid_shape():
    for extra in ({"pass": True}, {"cause": "qa_capability"}):
        check = {**_NOT_APPLICABLE_EMPTY_INPUT, **extra}
        result = parse_qa_result(json.dumps({"pass": True, "checks": [check], "summary": "s"}))
        assert result.passed is False
        assert result.blocker is not None
        assert "not-applicable check 0" in result.summary
    check = {**_NOT_APPLICABLE_EMPTY_INPUT, "not_applicable": False}
    result = parse_qa_result(json.dumps({"pass": True, "checks": [check], "summary": "s"}))
    assert result.blocker is not None


def test_pass_false_with_only_a_not_applicable_check_is_contradictory():
    raw = json.dumps({"pass": False, "checks": [_NOT_APPLICABLE_EMPTY_INPUT], "summary": "s"})

    result = parse_qa_result(raw)

    assert result.blocker is not None


def test_a_stored_run_result_from_before_the_change_still_parses():
    stored = QARunResult.model_validate(
        {
            "qa_outcome": "failed",
            "summary": "Empty input failed",
            "failed_checks": [
                {"name": "Empty input", "detail": "not sendable", "cause": "qa_capability"},
                {"name": "/start", "detail": "no reply"},
            ],
            "telegram_probe_evidence": [
                {
                    "action": "message",
                    "attempted": "send '' to @financebot",
                    "sent": "",
                    "delivered": False,
                    "replies": [],
                }
            ],
        }
    )

    assert [check.cause.value for check in stored.failed_checks] == ["qa_capability", "product"]


def test_the_prompt_carries_the_not_applicable_rules():
    prompt = " ".join(build_qa_prompt(CRITERIA, "https://bot.example.com", "financebot").split())

    assert (
        '{"name": "not applicable check", "not_applicable": true, "detail": "one-line summary"}'
        in prompt
    )
    assert "4. Edge cases you add beyond the acceptance criteria" in prompt
    assert "only inputs the transport can deliver" in prompt
    assert "Over Telegram, never send an empty or whitespace-only message" in prompt
    assert "empty input" not in prompt.lower().split("## checklist")[1].split("## report")[0]
    assert "report that check in the not-applicable form, never as failed" in prompt
    assert "An acceptance-criterion check is never not applicable" in prompt
    assert "A not-applicable check is not a failed check" in prompt
