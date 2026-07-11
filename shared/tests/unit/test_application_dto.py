"""Unit tests for Application DTOs — ApplicationDTO, ApplicationCreate, ApplicationUpdate."""

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
import pytest

from shared.contracts.dto.application import (
    ApplicationCreate,
    ApplicationDTO,
    ApplicationStatus,
    ApplicationUpdate,
)

_NOW = datetime(2026, 3, 17, tzinfo=UTC)


class TestApplicationDTO:
    """ApplicationDTO should parse API response dicts."""

    SAMPLE_RESPONSE: dict[str, Any] = {
        "id": 42,
        "repo_id": "repo-abc",
        "server_handle": "srv-1",
        "service_name": "backend",
        "status": "running",
        "last_health_check": _NOW.isoformat(),
        "response_time_ms": 150,
        "ssl_expires_at": _NOW.isoformat(),
        "uptime_pct_24h": 99.9,
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }

    def test_parse_full_response(self):
        dto = ApplicationDTO.model_validate(self.SAMPLE_RESPONSE)
        assert dto.id == 42
        assert dto.repo_id == "repo-abc"
        assert dto.server_handle == "srv-1"
        assert dto.service_name == "backend"
        assert dto.status == "running"
        assert dto.response_time_ms == 150
        assert dto.uptime_pct_24h == 99.9

    def test_parse_minimal_response(self):
        minimal = {
            "id": 1,
            "repo_id": "repo-min",
            "server_handle": "srv-2",
            "service_name": "api",
            "status": "not_deployed",
            "created_at": _NOW.isoformat(),
        }
        dto = ApplicationDTO.model_validate(minimal)
        assert dto.last_health_check is None
        assert dto.response_time_ms is None
        assert dto.ssl_expires_at is None
        assert dto.uptime_pct_24h is None

    def test_model_dump_roundtrip(self):
        dto = ApplicationDTO.model_validate(self.SAMPLE_RESPONSE)
        data = dto.model_dump(mode="json")
        dto2 = ApplicationDTO.model_validate(data)
        assert dto2.id == dto.id

    def test_status_is_typed_enum(self):
        dto = ApplicationDTO.model_validate(self.SAMPLE_RESPONSE)
        assert dto.status is ApplicationStatus.RUNNING

    def test_rejects_unknown_status(self):
        bad = {**self.SAMPLE_RESPONSE, "status": "crashed"}
        with pytest.raises(ValidationError):
            ApplicationDTO.model_validate(bad)


class TestApplicationCreate:
    def test_minimal(self):
        create = ApplicationCreate(
            repo_id="repo-1",
            server_handle="srv-1",
            service_name="backend",
        )
        data = create.model_dump(mode="json")
        assert data["status"] == "not_deployed"

    def test_with_status(self):
        create = ApplicationCreate(
            repo_id="repo-1",
            server_handle="srv-1",
            service_name="api",
            status=ApplicationStatus.RUNNING,
        )
        data = create.model_dump(mode="json")
        assert data["status"] == "running"


class TestApplicationUpdate:
    def test_exclude_unset(self):
        update = ApplicationUpdate(status=ApplicationStatus.DOWN, response_time_ms=500)
        data = update.model_dump(exclude_unset=True)
        assert data == {"status": "down", "response_time_ms": 500}

    def test_all_fields_optional(self):
        update = ApplicationUpdate()
        data = update.model_dump(exclude_unset=True)
        assert data == {}

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            ApplicationUpdate(status="crashed")
