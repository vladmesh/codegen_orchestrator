import json
from pathlib import Path

import pytest
from worker_wrapper.observability import extract_effort_metrics, redact_transcript, save_transcript

from shared.contracts.dto.engineering_attempt import ClaudeResultEvidence, FactoryResultEvidence

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_missing_usage_is_kept_absent() -> None:
    """A provider without usage data must not be recorded as zero usage."""
    assert extract_effort_metrics("not json", "", "codex") == {}


def _stream(*results: str) -> str:
    """A Claude stream-json stdout whose events before ``results`` carry no evidence."""
    return "\n".join(
        (
            '{"type":"system","subtype":"init","session_id":"s-1","model":"claude-sonnet-5"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}',
            *results,
        )
    )


def test_stream_result_evidence_equals_the_single_document_form() -> None:
    """The captured final-result event yields the same evidence inside its stream."""
    document = (FIXTURES / "claude_code_2.1.278_result.json").read_text(encoding="utf-8")
    stream = (FIXTURES / "claude_code_2.1.278_stream.jsonl").read_text(encoding="utf-8")

    from_stream = extract_effort_metrics(stream.strip(), "", "claude")["claude_evidence"]
    from_document = extract_effort_metrics(document.strip(), "", "claude")["claude_evidence"]

    assert from_stream == from_document
    assert from_stream.cost_microusd == 56_448
    assert from_stream.model == "claude-sonnet-5"


def test_a_stream_without_a_result_event_leaves_cost_unknown() -> None:
    stream = (FIXTURES / "claude_code_2.1.278_stream.jsonl").read_text(encoding="utf-8")
    without_result = "\n".join(stream.strip().splitlines()[:-1])

    assert extract_effort_metrics(without_result, "", "claude") == {}


def test_extracts_usage_from_the_stream_result_event() -> None:
    stdout = _stream(
        '{"type":"result","total_cost_usd":0.0123,'
        '"usage":{"input_tokens":120,"output_tokens":30},'
        '"modelUsage":{"claude-sonnet-4-20250514":{"inputTokens":120}}}'
    )

    metrics = extract_effort_metrics(stdout, "", "claude")
    assert isinstance(metrics["claude_evidence"], ClaudeResultEvidence)
    assert metrics["claude_evidence"].model_dump(mode="json") == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_microusd": 12_300,
    }


def test_claude_evidence_uses_decimal_micro_usd_and_forwards_cache_tokens() -> None:
    stdout = _stream(
        '{"type":"result","model":"claude-opus-4-1","total_cost_usd":0.0123455,'
        '"usage":{"input_tokens":120,"output_tokens":30,'
        '"cache_read_input_tokens":40,"cache_creation_input_tokens":50}}'
    )

    metrics = extract_effort_metrics(stdout, "", "claude")
    assert isinstance(metrics["claude_evidence"], ClaudeResultEvidence)
    assert metrics["claude_evidence"].model_dump(mode="json") == {
        "provider": "anthropic",
        "model": "claude-opus-4-1",
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cache_read_tokens": 40,
        "cache_write_tokens": 50,
        "cost_microusd": 12_346,
    }


def test_claude_malformed_money_keeps_valid_usage_as_unknown() -> None:
    stdout = _stream(
        '{"type":"result","total_cost_usd":"not-a-number",'
        '"usage":{"input_tokens":120,"output_tokens":30}}'
    )

    evidence = extract_effort_metrics(stdout, "", "claude")["claude_evidence"]
    assert isinstance(evidence, ClaudeResultEvidence)
    assert evidence.input_tokens == 120
    assert evidence.output_tokens == 30
    assert evidence.cost_microusd is None


def test_claude_result_records_are_not_combined() -> None:
    """Only the final result event is read; an earlier one lends it no cost or usage."""
    stdout = _stream(
        '{"type":"result","total_cost_usd":0.01,"usage":{"input_tokens":1}}',
        '{"type":"result","usage":{"output_tokens":2}}',
    )

    evidence = extract_effort_metrics(stdout, "", "claude")["claude_evidence"]
    assert (evidence.input_tokens, evidence.output_tokens) == (None, 2)
    assert evidence.cost_microusd is None


def test_factory_retains_only_one_typed_result_object_without_money() -> None:
    stdout = """{
      "type": "result",
      "model": "factory-model",
      "usage": {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 50
      },
      "cost_usd": 0.0123
    }"""

    metrics = extract_effort_metrics(stdout, "", "factory")

    assert isinstance(metrics["factory_evidence"], FactoryResultEvidence)
    assert metrics["factory_evidence"].model_dump(mode="json") == {
        "provider": "factory",
        "model": "factory-model",
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cache_read_tokens": 40,
        "cache_write_tokens": 50,
    }
    assert "cost_usd" not in metrics


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"input_tokens": 10, "total_tokens": 5},
            {"input_tokens": 10, "output_tokens": None, "total_tokens": None},
        ),
        (
            {"input_tokens": 100, "output_tokens": -5, "total_tokens": 95},
            {"input_tokens": 100, "output_tokens": None, "total_tokens": None},
        ),
        (
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 99},
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        ),
    ],
)
def test_factory_normalizes_partial_or_inconsistent_usage_before_evidence(
    usage: dict, expected: dict
) -> None:
    metrics = extract_effort_metrics(json.dumps({"type": "result", "usage": usage}), "", "factory")

    evidence = metrics["factory_evidence"]
    assert isinstance(evidence, FactoryResultEvidence)
    assert evidence.model_dump(include=set(expected)) == expected


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        'progress before result\n{"type":"result","usage":{"input_tokens":1}}',
        '{"type":"result","usage":{"input_tokens":1}}\n{"type":"result","usage":{"output_tokens":2}}',
        '{"type":"progress","usage":{"input_tokens":1}}',
        '{"type":"result","usage":{"input_tokens":-1}}',
    ],
)
def test_factory_malformed_or_ambiguous_output_yields_no_evidence(stdout: str) -> None:
    assert extract_effort_metrics(stdout, "", "factory") == {}


def test_transcript_redacts_environment_secret_values() -> None:
    transcript = redact_transcript(
        "Authorization: Bearer token-value", {"API_TOKEN": "token-value"}
    )
    assert "token-value" not in transcript
    assert "[redacted]" in transcript


def test_truncated_transcript_remains_valid_utf8(tmp_path) -> None:
    path, truncated = save_transcript(str(tmp_path), "worker", "request", "🙂" * 20, 50, {})

    assert truncated is True
    assert path is not None
    assert "[transcript truncated" in open(path, encoding="utf-8").read()
