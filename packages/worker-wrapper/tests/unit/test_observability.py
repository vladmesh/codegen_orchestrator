from worker_wrapper.observability import extract_effort_metrics, redact_transcript


def test_missing_usage_is_kept_absent() -> None:
    """A provider without usage data must not be recorded as zero usage."""
    assert extract_effort_metrics("not json", "", "codex") == {}


def test_transcript_redacts_environment_secret_values() -> None:
    transcript = redact_transcript(
        "Authorization: Bearer token-value", {"API_TOKEN": "token-value"}
    )
    assert "token-value" not in transcript
    assert "[redacted]" in transcript
