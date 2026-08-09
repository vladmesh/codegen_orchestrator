"""Authenticated worker-broker client. This is the wrapper's only control-plane transport."""

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import httpx

from shared.contracts.queues.worker_result import WorkerResult


@dataclass(frozen=True)
class BrokerMessage:
    message_id: str
    data: dict[str, str]


class WorkerBrokerClient:
    def __init__(self, base_url: str, token: str, worker_id: str):
        self._base_url = base_url.rstrip("/")
        self._worker_id = worker_id
        self._client = httpx.AsyncClient(timeout=180, headers={"X-Worker-Broker-Token": token})

    @property
    def _worker_url(self) -> str:
        return f"{self._base_url}/v1/workers/{self._worker_id}"

    async def close(self) -> None:
        await self._client.aclose()

    async def lease_input(self) -> BrokerMessage | None:
        response = await self._client.post(f"{self._worker_url}/input/lease")
        if response.status_code == HTTPStatus.NO_CONTENT:
            return None
        response.raise_for_status()
        payload = response.json()
        return BrokerMessage(message_id=payload["lease_id"], data=payload["data"])

    async def submit_output(self, lease_id: str, result: WorkerResult) -> None:
        response = await self._client.post(
            f"{self._worker_url}/output",
            json={"lease_id": lease_id, "result": result.model_dump(mode="json")},
        )
        response.raise_for_status()

    async def update_status(self, values: dict[str, str]) -> None:
        response = await self._client.post(f"{self._worker_url}/status", json={"values": values})
        response.raise_for_status()

    async def get_session(self) -> str | None:
        response = await self._client.get(f"{self._worker_url}/session")
        response.raise_for_status()
        return response.json()["session_id"]

    async def set_session(self, session_id: str) -> None:
        response = await self._client.put(
            f"{self._worker_url}/session", json={"session_id": session_id}
        )
        response.raise_for_status()

    async def clear_session(self) -> None:
        response = await self._client.delete(f"{self._worker_url}/session")
        response.raise_for_status()

    async def compose(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        response = await self._client.post(f"{self._worker_url}/infra/compose", json=request)
        return response.status_code, response.json()
