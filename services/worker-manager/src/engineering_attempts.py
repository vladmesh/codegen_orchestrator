"""Read the active engineering attempts used by worker inventory."""

from typing import Any

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.run import RunStatus, RunType


class EngineeringAttemptInventory(InternalAPIClient):
    """The worker manager's internal read of running engineering Run records."""

    async def list_running(self) -> list[dict[str, Any]]:
        response = await self.request(
            "GET",
            "runs/",
            params={"run_type": RunType.ENGINEERING.value, "status": RunStatus.RUNNING.value},
        )
        data = response.json()
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise RuntimeError("running engineering attempt inventory is not a list of objects")
        return data
