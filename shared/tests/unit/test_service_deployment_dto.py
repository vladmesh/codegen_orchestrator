"""Unit tests for ServiceDeploymentDTO — typed lifecycle field."""

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
import pytest

from shared.contracts.dto.deployment import DeploymentResult
from shared.contracts.dto.service_deployment import ServiceDeploymentDTO

_NOW = datetime(2026, 3, 17, tzinfo=UTC)

SAMPLE_RESPONSE: dict[str, Any] = {
    "id": 7,
    "project_id": "proj-abc",
    "server_id": 3,
    "service_name": "backend",
    "port": 8000,
    "status": "success",
    "url": "https://backend.example.com",
    "deployed_at": _NOW.isoformat(),
    "created_at": _NOW.isoformat(),
    "updated_at": _NOW.isoformat(),
}


class TestServiceDeploymentDTO:
    def test_status_is_typed_enum(self):
        dto = ServiceDeploymentDTO.model_validate(SAMPLE_RESPONSE)
        assert dto.status is DeploymentResult.SUCCESS
        assert dto.port == 8000

    def test_rejects_unknown_status(self):
        bad = {**SAMPLE_RESPONSE, "status": "half_deployed"}
        with pytest.raises(ValidationError):
            ServiceDeploymentDTO.model_validate(bad)
