"""Unit tests for report-blocker command."""

from unittest.mock import patch

from orchestrator_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


class TestReportBlocker:
    def test_outputs_blocked_marker(self):
        """Command outputs ## BLOCKED marker that worker-wrapper detects."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}):
            result = runner.invoke(app, ["report-blocker", "--reason", "URLs return 404"])
        assert result.exit_code == 0
        assert "## BLOCKED" in result.output
        assert "URLs return 404" in result.output

    def test_writes_blocker_md(self, tmp_path):
        """Command writes BLOCKER.md with reason."""
        blocker_path = tmp_path / "BLOCKER.md"
        with (
            patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}),
            patch(
                "orchestrator_cli.commands.report_blocker.BLOCKER_MD_PATH",
                str(blocker_path),
            ),
        ):
            result = runner.invoke(app, ["report-blocker", "-r", "Missing API key"])
        assert result.exit_code == 0
        assert blocker_path.exists()
        content = blocker_path.read_text()
        assert "Missing API key" in content

    def test_requires_reason_option(self):
        """Command fails without --reason."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}):
            result = runner.invoke(app, ["report-blocker"])
        assert result.exit_code != 0

    def test_permission_check(self):
        """Command respects ORCHESTRATOR_ALLOWED_TOOLS."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": "respond"}, clear=False):
            result = runner.invoke(app, ["report-blocker", "--reason", "test"])
        assert result.exit_code == 1
