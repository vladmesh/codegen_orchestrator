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

    @patch("orchestrator.commands.project.uuid")
    @patch("orchestrator.commands.project.client")
    def test_project_create_json(self, mock_client, mock_uuid):
        """project create --json returns JSON output with auto-generated UUID."""
        mock_uuid.uuid4.return_value.hex = "abc123"
        mock_client.post.return_value = {
            "id": "abc123",
            "name": "newproj",
            "status": "created",
        }

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            # create now only requires name - id is auto-generated
            result = runner.invoke(app, ["project", "create", "--name", "newproj", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "newproj"
        assert "id" in data

    @patch("orchestrator.commands.project.client")
    def test_project_create_validation_error_empty_name(self, mock_client):
        """project create with empty name fails validation."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(app, ["project", "create", "--name", ""])

        assert result.exit_code == 1
        assert "✗ name:" in result.output
        assert "at least 1 character" in result.output.lower()

    @patch("orchestrator.commands.project.client")
    def test_project_set_secret_json(self, mock_client):
        """project set-secret --json returns JSON output."""
        mock_client.get.return_value = {
            "id": "proj-123",
            "name": "my-project",
            "status": "active",
            "config": {},
        }
        mock_client.patch.return_value = {
            "id": "proj-123",
            "name": "my-project",
            "status": "active",
            "config": {"secrets": {"TELEGRAM_TOKEN": "secret-value"}},
        }

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(
                app,
                [
                    "project",
                    "set-secret",
                    "--project-id",
                    "proj-123",
                    "--key",
                    "TELEGRAM_TOKEN",
                    "--value",
                    "secret-value",
                    "--json",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["config"]["secrets"]["TELEGRAM_TOKEN"] == "secret-value"  # noqa: S105

    @patch("orchestrator.commands.project.client")
    def test_project_set_secret_validation_error_lowercase_key(self, mock_client):
        """project set-secret with lowercase key fails validation."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(
                app,
                [
                    "project",
                    "set-secret",
                    "--project-id",
                    "proj-123",
                    "--key",
                    "telegram_token",  # lowercase - should fail
                    "--value",
                    "secret",
                ],
            )

        assert result.exit_code == 1
        assert "✗ key:" in result.output
        assert "should match pattern" in result.output.lower()

    @patch("orchestrator.commands.project.client")
    def test_project_set_secret_preserves_existing_secrets(self, mock_client):
        """project set-secret preserves existing secrets in config."""
        mock_client.get.return_value = {
            "id": "proj-123",
            "name": "my-project",
            "status": "active",
            "config": {"secrets": {"EXISTING_SECRET": "existing-value"}},
        }
        mock_client.patch.return_value = {
            "id": "proj-123",
            "name": "my-project",
            "status": "active",
            "config": {
                "secrets": {
                    "EXISTING_SECRET": "existing-value",
                    "NEW_SECRET": "new-value",
                }
            },
        }

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(
                app,
                [
                    "project",
                    "set-secret",
                    "--project-id",
                    "proj-123",
                    "--key",
                    "NEW_SECRET",
                    "--value",
                    "new-value",
                ],
            )

        assert result.exit_code == 0
        # Verify PATCH was called with both secrets
        patch_call = mock_client.patch.call_args
        config = patch_call[1]["json"]["config"]
        assert "EXISTING_SECRET" in config["secrets"]
        assert "NEW_SECRET" in config["secrets"]


class TestRespondCommand:
    """Tests for respond command."""

    @patch("orchestrator.commands.answer._get_redis")
    def test_respond_allowed(self, mock_get_redis):
        """respond works when allowed."""
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        with patch.dict(
            "os.environ",
            {"ORCHESTRATOR_ALLOWED_TOOLS": "respond", "ORCHESTRATOR_AGENT_ID": "test-agent"},
            clear=False,
        ):
            result = runner.invoke(app, ["respond", "Hello user"])

        assert result.exit_code == 0
        assert "Answer sent" in result.output

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
            result = runner.invoke(app, ["deploy", "trigger", "--project-id", "123", "--json"])

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


class TestEngineeringCommands:
    """Tests for engineering commands."""

    @patch("orchestrator.commands.engineering._get_redis")
    @patch("orchestrator.commands.engineering.client")
    def test_engineering_trigger_json(self, mock_client, mock_redis):
        """engineering trigger --json returns JSON output."""
        mock_client.post.return_value = {}
        mock_redis_instance = MagicMock()
        mock_redis.return_value = mock_redis_instance

        with patch.dict(
            "os.environ",
            {"ORCHESTRATOR_ALLOWED_TOOLS": "", "ORCHESTRATOR_USER_ID": "test_user"},
            clear=False,
        ):
            result = runner.invoke(
                app, ["engineering", "trigger", "--project-id", "proj-123", "--json"]
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["project_id"] == "proj-123"
        assert data["status"] == "queued"
        assert "task_id" in data

    @patch("orchestrator.commands.engineering._get_redis")
    @patch("orchestrator.commands.engineering.client")
    def test_engineering_trigger_validation_error_empty_project_id(self, mock_client, mock_redis):
        """engineering trigger with empty project_id fails validation."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(app, ["engineering", "trigger", "--project-id", ""])

        assert result.exit_code == 1
        assert "✗ project_id:" in result.output
        assert "at least 1 character" in result.output.lower()

    @patch("orchestrator.commands.engineering.client")
    def test_engineering_status_json(self, mock_client):
        """engineering status --json returns JSON output."""
        mock_client.get.return_value = {
            "type": "engineering",
            "status": "running",
            "project_id": "proj-123",
            "created_at": "2026-01-01T00:00:00Z",
        }

        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(app, ["engineering", "status", "task-123", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "running"


class TestDeployValidation:
    """Tests for deploy command validation."""

    @patch("orchestrator.commands.deploy._get_redis")
    @patch("orchestrator.commands.deploy.client")
    def test_deploy_trigger_validation_error_empty_project_id(self, mock_client, mock_redis):
        """deploy trigger with empty project_id fails validation."""
        with patch.dict("os.environ", {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            result = runner.invoke(app, ["deploy", "trigger", "--project-id", ""])

        assert result.exit_code == 1
        assert "✗ project_id:" in result.output
        assert "at least 1 character" in result.output.lower()
