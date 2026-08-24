from worker_wrapper.observability import extract_effort_metrics, redact_transcript, save_transcript


def test_missing_usage_is_kept_absent() -> None:
    """A provider without usage data must not be recorded as zero usage."""
    assert extract_effort_metrics("not json", "", "codex") == {}


def test_extracts_usage_from_indented_claude_json() -> None:
    """Claude CLI writes one multi-line JSON result, not JSONL."""
    stdout = """{
      "type": "result",
      "total_cost_usd": 0.0123,
      "usage": {"input_tokens": 120, "output_tokens": 30},
      "modelUsage": {"claude-sonnet-4-20250514": {"inputTokens": 120}}
    }"""

    assert extract_effort_metrics(stdout, "", "claude") == {
        "claude_evidence": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "cost_microusd": 12_300,
        }
    }


def test_claude_evidence_uses_decimal_micro_usd_and_forwards_cache_tokens() -> None:
    stdout = """{
      "type": "result",
      "model": "claude-opus-4-1",
      "total_cost_usd": 0.0123455,
      "usage": {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 50
      }
    }"""

    assert extract_effort_metrics(stdout, "", "claude") == {
        "claude_evidence": {
            "provider": "anthropic",
            "model": "claude-opus-4-1",
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "cache_read_tokens": 40,
            "cache_write_tokens": 50,
            "cost_microusd": 12_346,
        }
    }


def test_claude_malformed_money_keeps_valid_usage_as_unknown() -> None:
    stdout = """{
      "type": "result",
      "total_cost_usd": "not-a-number",
      "usage": {"input_tokens": 120, "output_tokens": 30}
    }"""

    evidence = extract_effort_metrics(stdout, "", "claude")["claude_evidence"]
    assert evidence["input_tokens"] == 120
    assert evidence["output_tokens"] == 30
    assert evidence["cost_microusd"] is None


def test_claude_multiple_json_records_are_not_combined() -> None:
    stdout = "\n".join(
        (
            '{"type":"result","total_cost_usd":0.01,"usage":{"input_tokens":1}}',
            '{"type":"result","usage":{"output_tokens":2}}',
        )
    )

    assert extract_effort_metrics(stdout, "", "claude") == {}


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
