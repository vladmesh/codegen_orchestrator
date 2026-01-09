"""Unit tests for Pydantic models."""

from orchestrator.models.deploy import DeployStart
from orchestrator.models.engineering import EngineeringTask
from orchestrator.models.project import ProjectCreate, SecretSet
from pydantic import ValidationError
import pytest


class TestProjectCreate:
    """Tests for ProjectCreate model."""

    def test_valid_project_name(self):
        """Valid project name passes validation."""
        project = ProjectCreate(name="my-telegram-bot")
        assert project.name == "my-telegram-bot"

    def test_empty_name_fails(self):
        """Empty name fails min_length validation."""
        with pytest.raises(ValidationError) as exc_info:
            ProjectCreate(name="")

        errors = exc_info.value.errors()
        assert any("name" in str(err["loc"]) for err in errors)
        assert any("at least 1 character" in err["msg"].lower() for err in errors)

    def test_name_too_long_fails(self):
        """Name exceeding max_length fails validation."""
        long_name = "a" * 101  # Exceeds 100 character limit

        with pytest.raises(ValidationError) as exc_info:
            ProjectCreate(name=long_name)

        errors = exc_info.value.errors()
        assert any("name" in str(err["loc"]) for err in errors)
        assert any("at most 100 characters" in err["msg"].lower() for err in errors)

    def test_name_with_special_chars(self):
        """Name with special characters is allowed."""
        project = ProjectCreate(name="my-bot_v2.0")
        assert project.name == "my-bot_v2.0"


class TestSecretSet:
    """Tests for SecretSet model."""

    def test_valid_secret_uppercase(self):
        """Valid uppercase secret key passes validation."""
        secret = SecretSet(
            project_id="proj-123",
            key="TELEGRAM_TOKEN",
            value="123456:ABC-DEF",
        )
        assert secret.key == "TELEGRAM_TOKEN"
        assert secret.value == "123456:ABC-DEF"

    def test_valid_secret_with_underscores(self):
        """Secret key with underscores passes validation."""
        secret = SecretSet(
            project_id="proj-123",
            key="DATABASE_CONNECTION_URL",
            value="postgres://...",
        )
        assert secret.key == "DATABASE_CONNECTION_URL"

    def test_valid_secret_with_numbers(self):
        """Secret key with numbers passes validation."""
        secret = SecretSet(
            project_id="proj-123",
            key="API_KEY_V2",
            value="secret",
        )
        assert secret.key == "API_KEY_V2"

    def test_lowercase_key_fails(self):
        """Lowercase secret key fails pattern validation."""
        with pytest.raises(ValidationError) as exc_info:
            SecretSet(
                project_id="proj-123",
                key="telegram_token",  # lowercase
                value="secret",
            )

        errors = exc_info.value.errors()
        assert any("key" in str(err["loc"]) for err in errors)
        # Pydantic v2 message is "String should match pattern '^[A-Z_][A-Z0-9_]*$'"
        assert any("should match pattern" in err["msg"].lower() for err in errors)

    def test_mixed_case_key_fails(self):
        """Mixed case secret key fails pattern validation."""
        with pytest.raises(ValidationError) as exc_info:
            SecretSet(
                project_id="proj-123",
                key="TelegramToken",  # mixed case
                value="secret",
            )

        errors = exc_info.value.errors()
        assert any("key" in str(err["loc"]) for err in errors)

    def test_key_with_hyphens_fails(self):
        """Secret key with hyphens fails pattern validation."""
        with pytest.raises(ValidationError) as exc_info:
            SecretSet(
                project_id="proj-123",
                key="TELEGRAM-TOKEN",  # hyphens not allowed
                value="secret",
            )

        errors = exc_info.value.errors()
        assert any("key" in str(err["loc"]) for err in errors)

    def test_key_starting_with_number_fails(self):
        """Secret key starting with number fails pattern validation."""
        with pytest.raises(ValidationError) as exc_info:
            SecretSet(
                project_id="proj-123",
                key="2FA_SECRET",  # starts with number
                value="secret",
            )

        errors = exc_info.value.errors()
        assert any("key" in str(err["loc"]) for err in errors)

    def test_empty_value_fails(self):
        """Empty secret value fails min_length validation."""
        with pytest.raises(ValidationError) as exc_info:
            SecretSet(
                project_id="proj-123",
                key="TELEGRAM_TOKEN",
                value="",  # empty
            )

        errors = exc_info.value.errors()
        assert any("value" in str(err["loc"]) for err in errors)
        assert any("at least 1 character" in err["msg"].lower() for err in errors)

    def test_missing_project_id_fails(self):
        """Missing project_id fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            SecretSet(key="TELEGRAM_TOKEN", value="secret")  # type: ignore

        errors = exc_info.value.errors()
        assert any("project_id" in str(err["loc"]) for err in errors)


class TestEngineeringTask:
    """Tests for EngineeringTask model."""

    def test_valid_project_id(self):
        """Valid project ID passes validation."""
        task = EngineeringTask(project_id="abc-123")
        assert task.project_id == "abc-123"

    def test_valid_uuid_project_id(self):
        """Valid UUID project ID passes validation."""
        task = EngineeringTask(project_id="550e8400-e29b-41d4-a716-446655440000")
        assert task.project_id == "550e8400-e29b-41d4-a716-446655440000"

    def test_empty_project_id_fails(self):
        """Empty project ID fails min_length validation."""
        with pytest.raises(ValidationError) as exc_info:
            EngineeringTask(project_id="")

        errors = exc_info.value.errors()
        assert any("project_id" in str(err["loc"]) for err in errors)
        assert any("at least 1 character" in err["msg"].lower() for err in errors)

    def test_missing_project_id_fails(self):
        """Missing project_id fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            EngineeringTask()  # type: ignore

        errors = exc_info.value.errors()
        assert any("project_id" in str(err["loc"]) for err in errors)


class TestDeployStart:
    """Tests for DeployStart model."""

    def test_valid_project_id(self):
        """Valid project ID passes validation."""
        deploy = DeployStart(project_id="abc-123")
        assert deploy.project_id == "abc-123"

    def test_valid_uuid_project_id(self):
        """Valid UUID project ID passes validation."""
        deploy = DeployStart(project_id="550e8400-e29b-41d4-a716-446655440000")
        assert deploy.project_id == "550e8400-e29b-41d4-a716-446655440000"

    def test_empty_project_id_fails(self):
        """Empty project ID fails min_length validation."""
        with pytest.raises(ValidationError) as exc_info:
            DeployStart(project_id="")

        errors = exc_info.value.errors()
        assert any("project_id" in str(err["loc"]) for err in errors)
        assert any("at least 1 character" in err["msg"].lower() for err in errors)

    def test_missing_project_id_fails(self):
        """Missing project_id fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            DeployStart()  # type: ignore

        errors = exc_info.value.errors()
        assert any("project_id" in str(err["loc"]) for err in errors)
