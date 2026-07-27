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
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cost_usd": 0.0123,
    }


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
