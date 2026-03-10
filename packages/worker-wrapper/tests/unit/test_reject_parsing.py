"""Unit tests for REJECTED marker parsing in worker wrapper.

When a worker determines a CI failure is not a code issue (infrastructure,
missing secrets, orchestrator bug), it writes a ## REJECTED section in its
output. The wrapper must detect this and set status='rejected' + reject_reason.
"""

import json

from worker_wrapper.result_parser import ResultParser


class TestRejectMarkerInResultParser:
    """Tests for REJECTED marker detection in ResultParser."""

    def test_rejected_marker_in_plain_output(self):
        """## REJECTED in plain text output → status=rejected + reason."""
        stdout = (
            "I analyzed the CI failure.\n\n"
            "## REJECTED\n"
            "REGISTRY_PASSWORD secret is empty — this is an orchestrator "
            "configuration issue, not a code problem.\n"
            "Suggested action: Set REGISTRY_PASSWORD in project secrets."
        )
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "rejected"
        assert "REGISTRY_PASSWORD" in result["reject_reason"]

    def test_rejected_marker_in_claude_cli_json(self):
        """## REJECTED in Claude CLI JSON output → status=rejected."""
        cli_output = {
            "type": "result",
            "result": (
                "Analysis complete.\n\n"
                "## REJECTED\n"
                "Docker login failed because the registry TLS certificate "
                "is self-signed. Admin must configure cert trust.\n"
            ),
            "session_id": "test-session",
        }
        stdout = json.dumps(cli_output)
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "rejected"
        assert "TLS certificate" in result["reject_reason"]

    def test_rejected_extracts_multiline_reason(self):
        """Reject reason includes all text after ## REJECTED header."""
        stdout = (
            "Looked at the logs.\n\n"
            "## REJECTED\n"
            "Line 1 of reason.\n"
            "Line 2 of reason.\n"
            "Line 3 of reason."
        )
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "rejected"
        assert "Line 1" in result["reject_reason"]
        assert "Line 3" in result["reject_reason"]

    def test_no_rejected_marker_returns_none(self):
        """Without ## REJECTED and without <result> tags → None."""
        stdout = "Agent finished normally without any special markers."
        result = ResultParser.parse(stdout)
        assert result is None

    def test_result_tags_take_priority_over_rejected(self):
        """<result> tags with explicit JSON take priority over ## REJECTED."""
        stdout = (
            "## REJECTED\nSome reason\n\n"
            '<result>{"status": "success", "commit_sha": "abc123"}</result>'
        )
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "success"

    def test_rejected_reason_truncated_at_1000_chars(self):
        """Very long reject reasons are truncated."""
        long_reason = "x" * 2000
        stdout = f"## REJECTED\n{long_reason}"
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "rejected"
        assert len(result["reject_reason"]) <= 1000

    def test_rejected_marker_case_insensitive_header(self):
        """## Rejected (mixed case) should also be detected."""
        stdout = "## Rejected\nThis is a reject reason."
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "rejected"

    def test_extract_text_with_rejected_marker(self):
        """extract_text returns plain text but check_rejected detects marker."""
        cli_output = {
            "type": "result",
            "result": "## REJECTED\nInfra issue.",
            "session_id": "test",
        }
        stdout = json.dumps(cli_output)
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "rejected"


class TestRejectInWrapperOutput:
    """Tests for how wrapper.execute_agent enriches result with reject info."""

    def test_rejected_result_structure(self):
        """Rejected result must have status, reject_reason, no commit_sha."""
        stdout = "## REJECTED\nNot a code issue."
        result = ResultParser.parse(stdout)
        assert result["status"] == "rejected"
        assert "reject_reason" in result
        assert result.get("commit_sha") is None
