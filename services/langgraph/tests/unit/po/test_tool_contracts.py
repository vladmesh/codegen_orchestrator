"""Contract tests: validate PO tool payloads against API Pydantic schemas.

These tests import API schemas directly and validate that the payloads
constructed by PO tools conform to the schemas — catching type mismatches
(e.g. string vs UUID) without needing a running API.
"""

from __future__ import annotations

import sys
import uuid

from pydantic import ValidationError
import pytest

# API schemas live in services/api/src — add to path for cross-service import.
# In production, API validates these; here we validate PO tool payloads match.
_API_SRC = str(__import__("pathlib").Path(__file__).resolve().parents[4] / "api" / "src")
if _API_SRC not in sys.path:
    sys.path.insert(0, _API_SRC)

from schemas.project import MergeSecretsRequest, ProjectCreate  # noqa: E402
from schemas.story import StoryCreate  # noqa: E402


class TestCreateProjectPayload:
    """Payloads built by create_project tool must pass ProjectCreate validation."""

    def test_valid_payload(self):
        """Standard create_project payload validates successfully."""
        project_id = str(uuid.uuid4())
        payload = {
            "id": project_id,
            "name": "my-bot",
            "status": "draft",
            "config": {"modules": ["backend", "tg_bot"], "description": "A bot", "name": "my-bot"},
        }
        schema = ProjectCreate.model_validate(payload)
        assert schema.id == uuid.UUID(project_id)
        assert schema.name == "my-bot"

    def test_uuid_string_coerced(self):
        """ProjectCreate accepts UUID as string (Pydantic coerces it)."""
        payload = {
            "id": str(uuid.uuid4()),
            "name": "test",
            "status": "draft",
            "config": {},
        }
        schema = ProjectCreate.model_validate(payload)
        assert isinstance(schema.id, uuid.UUID)

    def test_invalid_id_rejected(self):
        """Non-UUID id is rejected by ProjectCreate."""
        payload = {
            "id": "abc123",
            "name": "test",
            "status": "draft",
            "config": {},
        }
        with pytest.raises(ValidationError, match="id"):
            ProjectCreate.model_validate(payload)

    def test_none_id_allowed(self):
        """ProjectCreate allows id=None (auto-generated)."""
        payload = {"name": "test", "status": "draft", "config": {}}
        schema = ProjectCreate.model_validate(payload)
        assert schema.id is None

    def test_config_accepts_arbitrary_dict(self):
        """The config field accepts any dict (modules, description, etc.)."""
        payload = {
            "id": str(uuid.uuid4()),
            "name": "test",
            "status": "draft",
            "config": {
                "modules": ["backend"],
                "description": "test desc",
                "name": "test",
                "extra_key": 42,
            },
        }
        schema = ProjectCreate.model_validate(payload)
        assert schema.config["modules"] == ["backend"]


class TestCreateStoryPayload:
    """Payloads built by create_story tool must pass StoryCreate validation."""

    def test_valid_payload(self):
        """Standard create_story payload validates successfully."""
        project_id = str(uuid.uuid4())
        payload = {
            "project_id": project_id,
            "title": "Create todo bot",
            "description": "Build a todo app with reminders",
            "type": "product",
            "created_by": "po",
        }
        schema = StoryCreate.model_validate(payload)
        assert schema.project_id == uuid.UUID(project_id)
        assert schema.title == "Create todo bot"
        assert schema.created_by == "po"

    def test_project_id_must_be_uuid(self):
        """StoryCreate rejects non-UUID project_id."""
        payload = {
            "project_id": "abc",
            "title": "Test",
            "description": "Test",
            "type": "product",
            "created_by": "po",
        }
        with pytest.raises(ValidationError, match="project_id"):
            StoryCreate.model_validate(payload)

    def test_type_must_be_valid_enum(self):
        """StoryCreate rejects invalid story type."""
        payload = {
            "project_id": str(uuid.uuid4()),
            "title": "Test",
            "type": "invalid_type",
            "created_by": "po",
        }
        with pytest.raises(ValidationError, match="type"):
            StoryCreate.model_validate(payload)

    def test_product_type_accepted(self):
        """StoryCreate accepts 'product' type (what PO tool sends)."""
        payload = {
            "project_id": str(uuid.uuid4()),
            "title": "Test",
            "type": "product",
            "created_by": "po",
        }
        schema = StoryCreate.model_validate(payload)
        assert schema.type == "product"

    def test_technical_type_accepted(self):
        """StoryCreate accepts 'technical' type."""
        payload = {
            "project_id": str(uuid.uuid4()),
            "title": "Test",
            "type": "technical",
            "created_by": "po",
        }
        schema = StoryCreate.model_validate(payload)
        assert schema.type == "technical"

    def test_description_optional(self):
        """StoryCreate allows missing description."""
        payload = {
            "project_id": str(uuid.uuid4()),
            "title": "Test",
            "type": "product",
            "created_by": "po",
        }
        schema = StoryCreate.model_validate(payload)
        assert schema.description is None


class TestMergeSecretsPayload:
    """Payloads built by set_project_secret tool must pass MergeSecretsRequest."""

    def test_valid_secrets_only(self):
        """Payload with secrets only (no hint)."""
        payload = {"secrets": {"TELEGRAM_BOT_TOKEN": "123:ABC"}}
        schema = MergeSecretsRequest.model_validate(payload)
        assert schema.secrets == {"TELEGRAM_BOT_TOKEN": "123:ABC"}
        assert schema.env_hints is None

    def test_secrets_with_hints(self):
        """Payload with both secrets and env_hints."""
        payload = {
            "secrets": {"ADMIN_ID": "42"},
            "env_hints": {"ADMIN_ID": "Telegram ID of the admin"},
        }
        schema = MergeSecretsRequest.model_validate(payload)
        assert schema.secrets["ADMIN_ID"] == "42"
        assert schema.env_hints["ADMIN_ID"] == "Telegram ID of the admin"

    def test_empty_secrets_rejected(self):
        """MergeSecretsRequest requires secrets dict (can be empty though)."""
        payload: dict = {}
        with pytest.raises(ValidationError, match="secrets"):
            MergeSecretsRequest.model_validate(payload)

    def test_secrets_values_must_be_strings(self):
        """Secret values must be strings, not ints."""
        payload = {"secrets": {"PORT": 8080}}
        with pytest.raises(ValidationError):
            MergeSecretsRequest.model_validate(payload)
