from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AllocationDTO(BaseModel):
    """Port allocation on a server."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    project_id: str
    service_name: str
    port: int
    allocated_at: datetime
