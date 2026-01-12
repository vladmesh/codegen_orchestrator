"""Minimal API client for infrastructure-worker.

Contains only methods needed for provisioning operations.
"""

from __future__ import annotations

import os

import httpx
import structlog

logger = structlog.get_logger(__name__)


class InfrastructureAPIClient:
    """HTTP client for infrastructure-worker's required API endpoints."""

    def __init__(self) -> None:
        api_base_url = os.getenv("API_BASE_URL", "http://api:8000")
        self.base_url = api_base_url.rstrip("/")
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

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_server(self, server_handle: str) -> dict:
        """Get server info by handle."""
        resp = await self._request("GET", f"servers/{server_handle}")
        return resp.json()

    async def update_server(self, server_handle: str, payload: dict) -> dict:
        """Update server fields."""
        resp = await self._request("PATCH", f"servers/{server_handle}", json=payload)
        return resp.json()

    async def get_server_services(self, server_handle: str) -> list[dict]:
        """Get list of services deployed on a server."""
        resp = await self._request("GET", f"servers/{server_handle}/services")
        return resp.json()


# Singleton instance
api_client = InfrastructureAPIClient()
