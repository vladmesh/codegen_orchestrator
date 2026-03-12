"""Unit tests for BLOCKED marker parsing in worker wrapper.

When a developer agent cannot solve a task (404 URLs, contradictory
requirements, missing credentials), it writes a ## BLOCKED section.
The wrapper detects this and sets status='blocked' + block_reason.
"""

import json

from worker_wrapper.result_parser import ResultParser


class TestBlockedMarkerInResultParser:
    """Tests for BLOCKED marker detection in ResultParser."""

    def test_blocked_marker_in_plain_output(self):
        """## BLOCKED in plain text output → status=blocked + reason."""
        stdout = (
            "I tried to download the images.\n\n"
            "## BLOCKED\n"
            "56/78 Minor Arcana URLs return 404. Only Major Arcana images are valid.\n"
            "Cannot complete the task without working image URLs."
        )
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "blocked"
        assert "56/78" in result["block_reason"]

    def test_blocked_marker_in_claude_cli_json(self):
        """## BLOCKED in Claude CLI JSON output → status=blocked."""
        cli_output = {
            "type": "result",
            "result": (
                "Attempted implementation.\n\n"
                "## BLOCKED\n"
                "API key for OpenRouter is not configured. Cannot generate predictions.\n"
            ),
            "session_id": "test-session",
        }
        stdout = json.dumps(cli_output)
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "blocked"
        assert "OpenRouter" in result["block_reason"]

    def test_blocked_extracts_multiline_reason(self):
        """Block reason includes all text after ## BLOCKED header."""
        stdout = "Analysis.\n\n## BLOCKED\nLine 1 of reason.\nLine 2 of reason.\nLine 3 of reason."
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "blocked"
        assert "Line 1" in result["block_reason"]
        assert "Line 3" in result["block_reason"]

    def test_result_tags_take_priority_over_blocked(self):
        """<result> tags with explicit JSON take priority over ## BLOCKED."""
        stdout = (
            "## BLOCKED\nSome reason\n\n"
            '<result>{"status": "success", "commit_sha": "abc123"}</result>'
        )
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "success"

    def test_rejected_takes_priority_over_blocked(self):
        """## REJECTED takes priority over ## BLOCKED."""
        stdout = "## REJECTED\nInfra issue.\n\n## BLOCKED\nDev issue."
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "rejected"

    def test_blocked_reason_truncated_at_1000_chars(self):
        """Very long block reasons are truncated."""
        long_reason = "x" * 2000
        stdout = f"## BLOCKED\n{long_reason}"
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "blocked"
        assert len(result["block_reason"]) <= 1000

    def test_blocked_marker_case_insensitive(self):
        """## Blocked (mixed case) should also be detected."""
        stdout = "## Blocked\nThis is a block reason."
        result = ResultParser.parse(stdout)
        assert result is not None
        assert result["status"] == "blocked"

    def test_blocked_result_structure(self):
        """Blocked result must have status, block_reason, no commit_sha."""
        stdout = "## BLOCKED\nNot solvable."
        result = ResultParser.parse(stdout)
        assert result["status"] == "blocked"
        assert "block_reason" in result
        assert result.get("commit_sha") is None
