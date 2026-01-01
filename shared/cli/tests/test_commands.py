"""Unit tests for CLI commands with mocked API."""

import json
from unittest.mock import MagicMock, patch

from orchestrator.main import app
from typer.testing import CliRunner

runner = CliRunner()

# Test constants
EXPECTED_PROJECT_COUNT = 2
EXPECTED_PROJECT_ID = 5


class TestProjectCommands:
    """Tests for project commands."""

    @patch("orchestrator.commands.project.client")
    def test_project_list_json(self, mock_client):
        """project list --json returns JSON output."""
        mock_client.get.return_value = [
            {"id": 1, "name": "project1", "status": "active"},
            {"id": 2, "name": "project2", "status": "deploying"},
        ]

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(app, ["project", "list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == EXPECTED_PROJECT_COUNT
        assert data[0]["name"] == "project1"

    @patch("orchestrator.commands.project.client")
    def test_project_list_table(self, mock_client):
        """project list without --json returns table."""
        mock_client.get.return_value = [
            {"id": 1, "name": "project1", "status": "active"},
        ]

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(app, ["project", "list"])

        assert result.exit_code == 0
        assert "project1" in result.output
        assert "active" in result.output

    @patch("orchestrator.commands.project.client")
    def test_project_get_json(self, mock_client):
        """project get --json returns JSON output."""
        mock_client.get.return_value = {
            "id": 1,
            "name": "myproject",
            "status": "active",
            "config": {"key": "value"},
        }

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(app, ["project", "get", "1", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "myproject"

    @patch("orchestrator.commands.project.client")
    def test_project_create_json(self, mock_client):
        """project create --json returns JSON output."""
        mock_client.post.return_value = {"id": 5, "name": "newproj", "status": "created"}

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            # create uses positional args: name, id
            result = runner.invoke(app, ["project", "create", "newproj", "5", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == EXPECTED_PROJECT_ID


class TestRespondCommand:
    """Tests for respond command."""

    def test_respond_allowed(self):
        """respond works when allowed."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": "respond"}, clear=False):
            result = runner.invoke(app, ["respond", "Hello user"])

        assert result.exit_code == 0
        assert "Response sent" in result.output
        assert "Hello user" in result.output

    def test_respond_denied(self):
        """respond fails when not allowed."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}, clear=False):
            result = runner.invoke(app, ["respond", "Hello user"])

        assert result.exit_code == 1
        assert "Permission Denied" in result.output


class TestDeployCommands:
    """Tests for deploy commands."""

    @patch("orchestrator.commands.deploy._get_redis")
    @patch("orchestrator.commands.deploy.client")
    def test_deploy_trigger_json(self, mock_client, mock_redis):
        """deploy trigger --json returns JSON output."""
        mock_client.post.return_value = {}
        mock_redis_instance = MagicMock()
        mock_redis.return_value = mock_redis_instance

        with patch.dict(
            "os.environ",
            {"ORCHESTRATOR_ALLOWED_TOOLS": "", "ORCHESTRATOR_USER_ID": "test_user"},
            clear=False,
        ):
            result = runner.invoke(app, ["deploy", "trigger", "123", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["project_id"] == "123"
        assert data["status"] == "queued"
        assert "task_id" in data

    @patch("orchestrator.commands.deploy.client")
    def test_deploy_status_json(self, mock_client):
        """deploy status --json returns JSON output."""
        mock_client.get.return_value = {
            "type": "deploy",
            "status": "running",
            "project_id": "123",
            "created_at": "2026-01-01T00:00:00Z",
        }

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(app, ["deploy", "status", "task-123", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "running"
