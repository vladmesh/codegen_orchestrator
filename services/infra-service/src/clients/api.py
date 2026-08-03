"""Minimal API client for infrastructure-worker.

Contains only methods needed for provisioning operations.
"""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
import os
from typing import Any

import httpx
from pydantic import BaseModel
import structlog

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.deployment import DeploymentResult
from shared.contracts.dto.incident import (
    IncidentCreate,
    IncidentDTO,
    IncidentStatus,
    IncidentType,
    IncidentUpdate,
)
from shared.contracts.dto.server import (
    ProvisioningAttemptReservationResult,
    ProvisioningAttemptResetResult,
    ServerDTO,
)

logger = structlog.get_logger(__name__)


class DeploymentRecord(BaseModel):
    """Deployment response needed by recovery, validated at the API boundary."""

    id: int
    project_id: str
    server_handle: str
    service_name: str
    port: int
    deployment_info: dict[str, Any]
    result: DeploymentResult
    deployed_at: datetime


class InfrastructureAPIClient(InternalAPIClient):
    """HTTP client for infrastructure-worker's required API endpoints."""

    def __init__(self) -> None:
        api_base_url = os.getenv("API_BASE_URL")
        if not api_base_url:
            raise RuntimeError("API_BASE_URL is not set")
        super().__init__(api_base_url)

    async def get_server(self, server_handle: str) -> ServerDTO:
        """Get server info by handle."""
        resp = await self.request("GET", f"servers/{server_handle}")
        return ServerDTO.model_validate(resp.json())

    async def get_server_ssh_key(self, server_handle: str) -> str | None:
        """Get a server's decrypted SSH private key, if one is stored."""
        try:
            resp = await self.request("GET", f"servers/{server_handle}/ssh-key")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise
        return resp.json().get("ssh_key")

    async def update_server(self, server_handle: str, payload: dict) -> dict:
        """Update server fields."""
        resp = await self.request("PATCH", f"servers/{server_handle}", json=payload)
        return resp.json()

    async def reserve_provisioning_attempt(
        self, server_handle: str, max_attempts: int
    ) -> ProvisioningAttemptReservationResult:
        """Reserve one attempt without a read-then-write race."""
        resp = await self.request(
            "POST",
            f"servers/{server_handle}/provisioning-attempts/reserve",
            json={"max_attempts": max_attempts},
        )
        return ProvisioningAttemptReservationResult.model_validate(resp.json())

    async def reset_provisioning_attempts(
        self, server_handle: str, attempt_number: int, episode_id: str
    ) -> ProvisioningAttemptResetResult:
        """Close an episode only if another attempt has not started."""
        resp = await self.request(
            "POST",
            f"servers/{server_handle}/provisioning-attempts/reset",
            json={"attempt_number": attempt_number, "episode_id": episode_id},
        )
        return ProvisioningAttemptResetResult.model_validate(resp.json())

    async def get_server_services(self, server_handle: str) -> list[DeploymentRecord]:
        """Get typed deployment records for a server."""
        resp = await self.request(
            "GET", "service-deployments/", params={"server_handle": server_handle}
        )
        return [DeploymentRecord.model_validate(item) for item in resp.json()]

    async def create_incident(self, incident: IncidentCreate) -> IncidentDTO:
        """Create an incident through the typed incident contract."""
        resp = await self.request("POST", "incidents/", json=incident.model_dump(mode="json"))
        return IncidentDTO.model_validate(resp.json())

    async def list_incidents(
        self,
        *,
        server_handle: str,
        status: IncidentStatus | None = None,
        incident_type: IncidentType | None = None,
    ) -> list[IncidentDTO]:
        """List incidents through the typed incident contract."""
        params = {"server_handle": server_handle}
        if status is not None:
            params["status"] = status.value
        if incident_type is not None:
            params["incident_type"] = incident_type.value
        resp = await self.request("GET", "incidents/", params=params)
        return [IncidentDTO.model_validate(item) for item in resp.json()]

    async def update_incident(self, incident_id: int, incident: IncidentUpdate) -> IncidentDTO:
        """Update an incident through the typed incident contract."""
        resp = await self.request(
            "PATCH",
            f"incidents/{incident_id}",
            json=incident.model_dump(mode="json", exclude_none=True),
        )
        return IncidentDTO.model_validate(resp.json())

    async def record_provisioning_failure(self, incident: IncidentCreate) -> IncidentDTO:
        """Atomically record a provisioning failure in its active episode."""
        if incident.incident_type is not IncidentType.PROVISIONING_FAILED:
            raise ValueError("record_provisioning_failure requires provisioning_failed")
        resp = await self.request(
            "POST", "incidents/provisioning-failure", json=incident.model_dump(mode="json")
        )
        return IncidentDTO.model_validate(resp.json())


# Singleton instance
api_client = InfrastructureAPIClient()
