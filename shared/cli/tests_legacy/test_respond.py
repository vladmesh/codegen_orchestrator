"""Unit tests for respond command."""

from unittest.mock import MagicMock, patch

from orchestrator.main import app
from typer.testing import CliRunner

runner = CliRunner()


class TestRespondCommand:
    """Tests for respond command with Redis publishing."""

    @patch("orchestrator.commands.answer._get_redis")
    def test_respond_publishes_answer(self, mock_get_redis):
        """respond publishes answer to Redis stream."""
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        with patch.dict(
            "os.environ",
            {
                "ORCHESTRATOR_ALLOWED_TOOLS": "",
                "ORCHESTRATOR_AGENT_ID": "test-agent-123",
            },
            clear=False,
        ):
            result = runner.invoke(app, ["respond", "Task completed"])

        assert result.exit_code == 0
        assert "Answer sent" in result.output

        # Verify Redis XADD call
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "cli-agent:responses"
        assert call_args[0][1]["type"] == "answer"
        assert call_args[0][1]["message"] == "Task completed"
        assert call_args[0][1]["agent_id"] == "test-agent-123"

    @patch("orchestrator.commands.answer._get_redis")
    def test_respond_with_expect_reply_publishes_question(self, mock_get_redis):
        """respond --expect-reply publishes question to Redis stream."""
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        with patch.dict(
            "os.environ",
            {
                "ORCHESTRATOR_ALLOWED_TOOLS": "",
                "ORCHESTRATOR_AGENT_ID": "test-agent-456",
            },
            clear=False,
        ):
            result = runner.invoke(app, ["respond", "What database?", "--expect-reply"])

        assert result.exit_code == 0
        assert "Question sent" in result.output

        # Verify Redis XADD call
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "cli-agent:responses"
        assert call_args[0][1]["type"] == "question"
        assert call_args[0][1]["question"] == "What database?"

    def test_respond_requires_agent_id(self):
        """respond fails if ORCHESTRATOR_AGENT_ID not set."""
        with patch.dict(
            "os.environ",
            {"ORCHESTRATOR_ALLOWED_TOOLS": ""},
            clear=False,
        ):
            # Remove ORCHESTRATOR_AGENT_ID if present
            import os

            os.environ.pop("ORCHESTRATOR_AGENT_ID", None)

            result = runner.invoke(app, ["respond", "Hello"])

        assert result.exit_code == 1
        assert "ORCHESTRATOR_AGENT_ID not set" in result.output

    def test_respond_permission_denied(self):
        """respond fails when not allowed by permissions."""
        with patch.dict(
            "os.environ",
            {
                "ORCHESTRATOR_ALLOWED_TOOLS": "project",  # respond not allowed
                "ORCHESTRATOR_AGENT_ID": "test-agent",
            },
            clear=False,
        ):
            result = runner.invoke(app, ["respond", "Hello"])

        assert result.exit_code == 1
        assert "Permission Denied" in result.output

    @patch("orchestrator.commands.answer._get_redis")
    def test_respond_short_flag(self, mock_get_redis):
        """respond -q works as shorthand for --expect-reply."""
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        with patch.dict(
            "os.environ",
            {
                "ORCHESTRATOR_ALLOWED_TOOLS": "",
                "ORCHESTRATOR_AGENT_ID": "test-agent",
            },
            clear=False,
        ):
            result = runner.invoke(app, ["respond", "Question?", "-q"])

        assert result.exit_code == 0
        call_args = mock_redis.xadd.call_args
        assert call_args[0][1]["type"] == "question"
