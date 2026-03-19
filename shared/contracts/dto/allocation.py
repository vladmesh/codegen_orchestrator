from datetime import datetime

from shared.contracts.dto.base import TimestampedDTO


class AllocationDTO(TimestampedDTO):
    """Port allocation on a server."""

    id: int
    server_id: int
    project_id: str
    service_name: str
    port: int
    allocated_at: datetime
