"""Service-specific API client for Scheduler."""

from __future__ import annotations

import httpx

from src.config import get_settings


class SchedulerAPIClient:
    """HTTP client for scheduler-required API endpoints."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.api_base_url.rstrip("/")
        if self.base_url.endswith("/api"):
            raise RuntimeError("API_BASE_URL must not include /api")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=30.0,
            )
        return self._client

    def _api_path(self, path: str) -> str:
        cleaned = path.lstrip("/")
        if cleaned.startswith("api/"):
            raise ValueError("API path should not include /api prefix")
        return f"/api/{cleaned}"

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        client = await self._get_client()
        resp = await client.request(method, self._api_path(path), **kwargs)
        resp.raise_for_status()
        return resp

    async def ingest_rag(self, body: bytes, headers: dict) -> dict:
        resp = await self._request("POST", "rag/ingest", content=body, headers=headers)
        return resp.json()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


api_client = SchedulerAPIClient()
