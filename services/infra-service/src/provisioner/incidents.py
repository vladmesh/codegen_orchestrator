"""Incident journal operations for provisioning failures."""

from datetime import UTC, datetime
from typing import Any

from shared.contracts.dto.incident import (
    IncidentCreate,
    IncidentStatus,
    IncidentType,
    IncidentUpdate,
)
from shared.log_config import get_logger
from src.clients.api import api_client

logger = get_logger(__name__)

_MAX_DIAGNOSTIC_LENGTH = 512
_SENSITIVE_KEYS = {"authorization", "credential", "password", "secret", "token", "api_key"}


class IncidentPersistenceError(RuntimeError):
    """The required incident journal write could not be completed."""

    def __init__(self, server_handle: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("Failed to persist provisioning incident")
        self.server_handle = server_handle
        self.details = _safe_diagnostics(details or {})


def _safe_diagnostics(value: Any, key: str | None = None) -> Any:
    """Keep incident diagnostics bounded and free of credential-like fields."""
    if key and any(fragment in key.lower() for fragment in _SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _safe_diagnostics(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_diagnostics(item) for item in value[:20]]
    if isinstance(value, str) and len(value) > _MAX_DIAGNOSTIC_LENGTH:
        return f"{value[:_MAX_DIAGNOSTIC_LENGTH]}…"
    return value


async def create_incident(
    server_handle: str,
    incident_type: IncidentType,
    details: dict[str, Any],
    affected_services: list[str] | None = None,
) -> None:
    """Persist a provisioning failure, raising when its mandatory journal write fails."""
    if incident_type is not IncidentType.PROVISIONING_FAILED:
        raise ValueError("Provisioner failures must use IncidentType.PROVISIONING_FAILED")
    incident = IncidentCreate(
        server_handle=server_handle,
        incident_type=incident_type,
        details=_safe_diagnostics(details),
        affected_services=affected_services or [],
    )
    try:
        recorded = await api_client.record_provisioning_failure(incident)
    except Exception as exc:
        logger.error(
            "incident_journal_write_failed",
            server_handle=server_handle,
            incident_type=incident_type.value,
            error_type=type(exc).__name__,
        )
        raise IncidentPersistenceError(server_handle, details) from exc
    logger.info(
        "incident_journal_recorded",
        incident_id=recorded.id,
        server_handle=server_handle,
        recovery_attempts=recorded.recovery_attempts,
    )


async def resolve_active_incidents(server_handle: str) -> None:
    """Resolve only active provisioning failures after successful provisioning."""
    incidents = []
    for incident_status in (IncidentStatus.DETECTED, IncidentStatus.RECOVERING):
        incidents.extend(
            await api_client.list_incidents(
                server_handle=server_handle,
                status=incident_status,
                incident_type=IncidentType.PROVISIONING_FAILED,
            )
        )
    resolved_at = datetime.now(UTC)
    for incident in incidents:
        await api_client.update_incident(
            incident.id,
            IncidentUpdate(status=IncidentStatus.RESOLVED, resolved_at=resolved_at),
        )
        logger.info("incident_resolved", incident_id=incident.id, server_handle=server_handle)
