"""Unit tests for Incident DTOs — IncidentDTO, IncidentCreate, IncidentUpdate + enums."""

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
import pytest

from shared.contracts.dto.incident import (
    IncidentCreate,
    IncidentDTO,
    IncidentStatus,
    IncidentType,
    IncidentUpdate,
)

_NOW = datetime(2026, 3, 17, tzinfo=UTC)


class TestIncidentEnums:
    def test_incident_status_values(self):
        assert IncidentStatus.DETECTED == "detected"
        assert IncidentStatus.RECOVERING == "recovering"
        assert IncidentStatus.RESOLVED == "resolved"
        assert IncidentStatus.FAILED == "failed"

    def test_incident_type_values(self):
        assert IncidentType.SERVER_UNREACHABLE == "server_unreachable"
        assert IncidentType.SERVICE_DOWN == "service_down"
        assert IncidentType.SSL_EXPIRING == "ssl_expiring"


class TestIncidentDTO:
    """IncidentDTO should parse API response dicts."""

    SAMPLE_RESPONSE: dict[str, Any] = {
        "id": 99,
        "server_handle": "srv-1",
        "incident_type": "server_unreachable",
        "status": "detected",
        "detected_at": _NOW.isoformat(),
        "resolved_at": None,
        "details": {"error": "SSH timeout"},
        "affected_services": ["backend", "frontend"],
        "recovery_attempts": 2,
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }

    def test_parse_full_response(self):
        dto = IncidentDTO.model_validate(self.SAMPLE_RESPONSE)
        assert dto.id == 99
        assert dto.server_handle == "srv-1"
        assert dto.incident_type == "server_unreachable"
        assert dto.status == "detected"
        assert dto.recovery_attempts == 2
        assert dto.affected_services == ["backend", "frontend"]
        assert dto.details == {"error": "SSH timeout"}

    def test_parse_resolved_incident(self):
        resolved = {
            **self.SAMPLE_RESPONSE,
            "status": "resolved",
            "resolved_at": _NOW.isoformat(),
            "recovery_attempts": 3,
        }
        dto = IncidentDTO.model_validate(resolved)
        assert dto.status == "resolved"
        assert dto.resolved_at is not None

    def test_model_dump_roundtrip(self):
        dto = IncidentDTO.model_validate(self.SAMPLE_RESPONSE)
        data = dto.model_dump(mode="json")
        dto2 = IncidentDTO.model_validate(data)
        assert dto2.id == dto.id

    def test_status_and_type_are_typed_enums(self):
        dto = IncidentDTO.model_validate(self.SAMPLE_RESPONSE)
        assert dto.status is IncidentStatus.DETECTED
        assert dto.incident_type is IncidentType.SERVER_UNREACHABLE

    def test_rejects_unknown_status(self):
        bad = {**self.SAMPLE_RESPONSE, "status": "acknowledged"}
        with pytest.raises(ValidationError):
            IncidentDTO.model_validate(bad)

    def test_rejects_unknown_incident_type(self):
        bad = {**self.SAMPLE_RESPONSE, "incident_type": "disk_full"}
        with pytest.raises(ValidationError):
            IncidentDTO.model_validate(bad)


class TestIncidentCreate:
    def test_minimal(self):
        create = IncidentCreate(
            server_handle="srv-1",
            incident_type=IncidentType.SERVER_UNREACHABLE,
        )
        data = create.model_dump(mode="json")
        assert data["server_handle"] == "srv-1"
        assert data["incident_type"] == "server_unreachable"
        assert data["details"] == {}
        assert data["affected_services"] == []

    def test_full(self):
        create = IncidentCreate(
            server_handle="srv-2",
            incident_type=IncidentType.SERVICE_DOWN,
            details={"service": "api"},
            affected_services=["api"],
        )
        data = create.model_dump(mode="json")
        assert data["affected_services"] == ["api"]


class TestIncidentUpdate:
    def test_exclude_unset(self):
        update = IncidentUpdate(
            status=IncidentStatus.RESOLVED,
            resolved_at=_NOW,
        )
        data = update.model_dump(exclude_unset=True, mode="json")
        assert data["status"] == "resolved"
        assert "resolved_at" in data

    def test_all_fields_optional(self):
        update = IncidentUpdate()
        data = update.model_dump(exclude_unset=True)
        assert data == {}
