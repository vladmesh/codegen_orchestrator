from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ServiceDeploymentDTO(BaseModel):
    """Service Deployment response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    server_id: int
    service_name: str
    port: int
    status: str
    url: str | None = None
    deployed_at: datetime
