"""Service-specific API client for LangGraph."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx

from src.config.settings import get_settings


class LanggraphAPIClient:
    """HTTP client for LangGraph's required API endpoints."""

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

    async def get(self, path: str, headers: dict | None = None, **kwargs) -> dict | list:
        resp = await self._request("GET", path, headers=headers, **kwargs)
        return resp.json()

    async def post(self, path: str, headers: dict | None = None, **kwargs) -> dict:
        resp = await self._request("POST", path, headers=headers, **kwargs)
        return resp.json()

    async def patch(self, path: str, headers: dict | None = None, **kwargs) -> dict:
        resp = await self._request("PATCH", path, headers=headers, **kwargs)
        return resp.json()

    async def delete(self, path: str, headers: dict | None = None, **kwargs) -> dict | None:
        resp = await self._request("DELETE", path, headers=headers, **kwargs)
        if resp.status_code == httpx.codes.NO_CONTENT:
            return None
        return resp.json()

    async def get_raw(self, path: str, **kwargs) -> httpx.Response:
        client = await self._get_client()
        resp = await client.get(self._api_path(path), **kwargs)
        return resp

    async def _get_json(self, path: str, **kwargs) -> dict | list:
        resp = await self._request("GET", path, **kwargs)
        return resp.json()

    async def _post_json(self, path: str, **kwargs) -> dict:
        resp = await self._request("POST", path, **kwargs)
        return resp.json()

    async def _patch_json(self, path: str, **kwargs) -> dict:
        resp = await self._request("PATCH", path, **kwargs)
        return resp.json()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_agent_config(self, agent_id: str) -> dict[str, Any]:
        return await self._get_json(f"agent-configs/{agent_id}")

    async def get_cli_agent_config(self, agent_id: str) -> dict[str, Any]:
        return await self._get_json(f"cli-agent-configs/{agent_id}")

    async def list_projects(self) -> list[dict]:
        return await self._get_json("projects/")

    async def list_servers(self, is_managed: bool | None = None) -> list[dict]:
        params = {}
        if is_managed is not None:
            params["is_managed"] = "true" if is_managed else "false"
        return await self._get_json("servers/", params=params)

    async def get_server(self, server_handle: str) -> dict:
        return await self._get_json(f"servers/{server_handle}")

    async def get_user_by_telegram(self, telegram_id: int) -> dict:
        return await self._get_json(f"users/by-telegram/{telegram_id}")

    async def update_server(self, server_handle: str, payload: dict) -> dict:
        return await self._patch_json(f"servers/{server_handle}", json=payload)

    async def get_server_services(self, server_handle: str) -> list[dict]:
        return await self._get_json(f"servers/{server_handle}/services")

    async def list_server_ports(self, server_handle: str) -> list[dict]:
        return await self._get_json(f"servers/{server_handle}/ports")

    async def allocate_server_port(self, server_handle: str, payload: dict) -> dict:
        return await self._post_json(f"servers/{server_handle}/ports", json=payload)

    async def create_service_deployment(self, payload: dict) -> dict:
        return await self._post_json("service-deployments/", json=payload)

    async def query_rag(self, payload: dict) -> dict:
        return await self._post_json("rag/query", json=payload)

    async def create_incident(self, payload: dict) -> dict:
        return await self._post_json("incidents/", json=payload)

    async def list_active_incidents(self) -> list[dict]:
        return await self._get_json("incidents/active")

    async def list_incidents(self, params: dict) -> list[dict]:
        return await self._get_json("incidents/", params=params)

    async def update_incident(self, incident_id: int, payload: dict) -> dict:
        return await self._patch_json(f"incidents/{incident_id}", json=payload)

    # --- Phase 4: Project methods ---

    async def get_project(self, project_id: str) -> dict | None:
        """Get a single project by ID."""
        try:
            return await self._get_json(f"projects/{project_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise

    # --- Phase 4: Allocation methods ---

    async def get_project_allocations(self, project_id: str) -> list[dict]:
        """Get all port allocations for a project."""
        return await self._get_json("allocations/", params={"project_id": project_id})

    async def get_allocation(self, allocation_id: int) -> dict | None:
        """Get a single allocation by ID."""
        try:
            return await self._get_json(f"allocations/{allocation_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise

    async def release_allocation(self, allocation_id: int) -> bool:
        """Release a port allocation."""
        try:
            await self.delete(f"allocations/{allocation_id}")
            return True
        except httpx.HTTPStatusError:
            return False


api_client = LanggraphAPIClient()
