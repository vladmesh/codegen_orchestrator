from datetime import datetime

from shared.contracts.dto.base import TimestampedDTO


class ServiceDeploymentDTO(TimestampedDTO):
    """Service Deployment response."""

    id: int
    project_id: str
    server_id: int
    service_name: str
    port: int
    status: str
    url: str | None = None
    deployed_at: datetime
