"""A scripted QA executor judges the 2026-09-17 balance check the way the prompt requires.

QA always acts as one Telegram identity, and round 1's records stayed in the
product: round 2 read a starting balance of 9400 and got 14100 after income 5000
and expense 300, and reported a correct bot as failing "not required 4700". No
harness runs the LLM executor deterministically, so the executor here is
scripted to follow the prompt's "Accumulated state" rule, and what it submits
goes through the real result parser.
"""

from __future__ import annotations

import json

from src.consumers._qa_runner import parse_qa_result
from src.prompts.qa import build_qa_prompt

CRITERION = "- после дохода 5000 и расхода 300 /balance отвечает 4700"
CHANGE = 4700


def _scripted_balance_check(start: int, after: int) -> dict:
    """Read the start, run the sequence, judge the change — the prompt's three steps."""
    expected = start + CHANGE
    if after == expected:
        return {
            "name": "balance after income and expense",
            "pass": True,
            "detail": f"start {start} read by /balance; after the sequence: {after}",
        }
    return {
        "name": "balance after income and expense",
        "pass": False,
        "detail": (
            f"expected: /balance отвечает {expected} (start {start} + {CHANGE}); received: {after}"
        ),
        "cause": "product",
    }


def _judge(start: int, after: int):
    check = _scripted_balance_check(start, after)
    raw = json.dumps({"pass": check["pass"], "checks": [check], "summary": "balance"})
    return parse_qa_result(raw)


def test_the_scripted_steps_are_the_rule_the_built_prompt_carries():
    prompt = " ".join(build_qa_prompt(CRITERION, "https://bot.example.com").split())

    assert "First read the starting value through the same observable" in prompt
    assert "A start of 9400 and a reply of 14100 in that form passes; a reply of 9400 fails." in (
        prompt
    )
    assert "names the observed starting value" in prompt


def test_the_round_two_observation_of_a_correct_bot_is_not_a_product_failure():
    result = _judge(start=9400, after=14100)

    assert result.passed is True
    assert result.blocker is None
    assert all(check["pass"] for check in result.checks)
    assert not any(check.get("cause") == "product" for check in result.checks)


def test_a_balance_that_did_not_change_still_fails_quoting_both_values():
    result = _judge(start=9400, after=9400)

    assert result.passed is False
    assert result.blocker is None
    [check] = result.checks
    assert check["cause"] == "product"
    assert check["detail"].startswith("expected: /balance отвечает 14100 (start 9400 + 4700)")
    assert check["detail"].endswith("; received: 9400")
