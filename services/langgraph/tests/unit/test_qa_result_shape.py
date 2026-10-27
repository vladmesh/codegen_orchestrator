"""Every invalid value in an executor's result is a named refusal, never an exception."""

from __future__ import annotations

import json

import pytest

from src.consumers._qa_result_shape import QAResultInvalid, decode_qa_result


def _failed(**fields) -> dict:
    return {"name": "health", "pass": False, "detail": "down", "cause": "product"} | fields


def _result(*checks, **fields) -> str:
    return json.dumps({"pass": False, "checks": list(checks), "summary": "bad"} | fields)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (_result(_failed(cause=["product"])), "failed check 0 cause must be one of"),
        (_result(_failed(cause={"kind": "product"})), "failed check 0 cause must be one of"),
        (_result(_failed(cause=7)), "failed check 0 cause must be one of"),
        (_result(_failed(name=42)), "check 0 name must be a non-empty string"),
        (_result(_failed(detail=["down"])), "check 0 detail must be a non-empty string"),
        (_result(_failed(), _failed(**{"pass": "no"})), "check 1 pass must be a boolean"),
        (_result(_failed(), 42), "check 1 must be an object"),
        (_result(_failed(), ["health"]), "check 1 must be an object"),
        (_result(_failed(telegram_step="1")), "check 0 telegram_step must be a positive integer"),
        (
            _result({"name": "empty", "not_applicable": "yes", "detail": "x"}),
            "not-applicable check 0 must contain exactly",
        ),
        (_result(**{"pass": "false"}), "pass must be a boolean"),
        (_result(summary=["bad"]), "summary must be a string"),
        (_result(checks={"health": "down"}), "checks must be a list"),
        (json.dumps({"type": "result", "result": {}}), "the result wrapper's result field"),
        (json.dumps({"type": "result", "result": ["x"]}), "the result wrapper's result field"),
        (json.dumps([_failed()]), "the result is not a JSON object"),
        ('{"pass": ' + "9" * 5000 + "}", "the result is not JSON"),
        ("[" * 100_000 + "]" * 100_000, "the result is not JSON"),
    ],
    ids=[
        "list-cause",
        "object-cause",
        "number-cause",
        "number-name",
        "list-detail",
        "string-pass-on-check",
        "number-check",
        "list-check",
        "string-telegram-step",
        "string-not-applicable",
        "string-top-level-pass",
        "list-summary",
        "object-checks",
        "object-wrapper-result",
        "list-wrapper-result",
        "list-result",
        "integer-over-digit-limit",
        "nested-beyond-recursion",
    ],
)
def test_an_invalid_value_is_refused_by_name(raw, reason):
    with pytest.raises(QAResultInvalid) as refused:
        decode_qa_result(raw)

    assert reason in str(refused.value)
