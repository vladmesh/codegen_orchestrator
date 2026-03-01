from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class IncidentSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class IncidentDTO(BaseModel):
    """Incident response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    severity: IncidentSeverity
    status: IncidentStatus
    title: str
    description: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
