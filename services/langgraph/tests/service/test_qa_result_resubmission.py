"""An invalid QA result is refused by name at `qa finish`, then kept as a blocker.

These drive the real loopback HTTP capability endpoint, through the unit modules'
`endpoint` and `central_run` harnesses, so they run in this service shard rather
than the host unit profile.
"""

from __future__ import annotations

import json

import pytest

from shared.contracts.dto.run_result import QABlockerCategory
from src.agents.qa.capability_service import MAX_RESULT_REFUSALS
from tests.unit.test_qa_capability_endpoint import PASSING_JSON, _call, endpoint as unit_endpoint
from tests.unit.test_qa_central_runtime import central_run as unit_central_run

pytestmark = pytest.mark.asyncio
endpoint = unit_endpoint
central_run = unit_central_run


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (
            {"pass": True, "checks": [], "summary": "OK", "report": "# QA"},
            "unexpected top-level keys: report",
        ),
        ({"pass": True, "checks": []}, "missing top-level keys: summary"),
        (
            {"pass": True, "checks": [{"name": "health", "pass": True}], "summary": "OK"},
            "passed check 0 must contain exactly name, pass, and detail fields",
        ),
        (
            {
                "pass": False,
                "checks": [
                    {"name": "health", "pass": False, "detail": "down", "cause": ["product"]}
                ],
                "summary": "bad",
            },
            "failed check 0 cause must be one of",
        ),
        (
            {"type": "result", "result": {}},
            "the result wrapper's result field must be a string",
        ),
    ],
    ids=["extra-key", "missing-key", "bad-check", "list-cause", "object-wrapper-result"],
)
async def test_an_invalid_result_is_refused_by_name_and_the_run_goes_on(endpoint, result, reason):
    status, body = await _call(endpoint, "submit_qa_result", {"result": json.dumps(result)})

    assert status == 400
    assert reason in body["error"]
    assert endpoint.service.results_refused == 1
    assert endpoint.service.verdict_received.is_set() is False
    assert endpoint.workspace.verdict is None

    status, _ = await _call(endpoint, "submit_qa_result", {"result": PASSING_JSON})

    assert status == 200
    assert endpoint.workspace.verdict == PASSING_JSON
    assert endpoint.service.verdict_received.is_set()


async def test_an_invalid_result_after_the_bounded_refusals_ends_the_run(endpoint):
    invalid = json.dumps(
        {
            "pass": False,
            "checks": [{"name": "health", "pass": False, "detail": "down", "cause": {}}],
            "summary": "bad",
        }
    )
    for _ in range(MAX_RESULT_REFUSALS):
        status, _ = await _call(endpoint, "submit_qa_result", {"result": invalid})
        assert status == 400

    status, _ = await _call(endpoint, "submit_qa_result", {"result": invalid})

    assert status == 200
    assert endpoint.workspace.verdict == invalid
    assert endpoint.service.verdict_received.is_set()


async def test_exhausted_resubmissions_end_the_run_with_the_whole_answer(central_run):
    checks = [{"name": f"check {i}", "pass": True, "detail": "ok " * 20} for i in range(40)]
    invalid = json.dumps({"pass": True, "checks": checks, "report": "# QA"})
    refusals = []

    async def behaviour(harness):
        for _ in range(MAX_RESULT_REFUSALS):
            refusals.append((await harness.submit(invalid))["error"])
        return invalid

    result, _, _, _ = await central_run(behaviour=behaviour)

    assert all("unexpected top-level keys: report" in refusal for refusal in refusals)
    assert result.passed is False
    assert result.blocker.category is QABlockerCategory.QA_RESULT_INVALID
    assert result.blocker.attempted == "validate QA agent result"
    assert result.blocker.received.startswith(
        "expected exactly pass, checks, and summary fields; "
        "unexpected top-level keys: report; missing top-level keys: summary"
    )
    assert invalid in result.blocker.received
