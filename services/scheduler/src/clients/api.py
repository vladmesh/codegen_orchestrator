"""Service-specific API client for Scheduler."""

from __future__ import annotations

import httpx

from shared.contracts.dto.api_key import APIKeyDTO
from shared.contracts.dto.project import ProjectCreate, ProjectDTO, ProjectUpdate
from shared.contracts.dto.server import ServerCreate, ServerDTO, ServerUpdate
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

    # --- Projects ---

    async def get_project_by_repo_id(self, repo_id: int) -> ProjectDTO | None:
        try:
            resp = await self._request("GET", f"projects/by-repo-id/{repo_id}")
            return ProjectDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    async def get_project_by_name(self, name: str) -> ProjectDTO | None:
        try:
            resp = await self._request("GET", "projects", params={"name": name})
            projects = [ProjectDTO.model_validate(p) for p in resp.json()]
            return projects[0] if projects else None
        except httpx.HTTPStatusError:
            # Depending on API impl, filtering might return empty list or 404
            return None

    async def get_projects(self) -> list[ProjectDTO]:
        resp = await self._request("GET", "projects")
        return [ProjectDTO.model_validate(p) for p in resp.json()]

    async def create_project(self, project: ProjectCreate) -> ProjectDTO:
        resp = await self._request("POST", "projects", json=project.model_dump())
        return ProjectDTO.model_validate(resp.json())

    async def update_project(self, project_id: str, project: ProjectUpdate) -> ProjectDTO:
        resp = await self._request(
            "PATCH", f"projects/{project_id}", json=project.model_dump(exclude_unset=True)
        )
        return ProjectDTO.model_validate(resp.json())

    # --- Servers ---

    async def get_servers(self) -> list[ServerDTO]:
        resp = await self._request("GET", "servers")
        return [ServerDTO.model_validate(s) for s in resp.json()]

    async def create_server(self, server: ServerCreate) -> ServerDTO:
        resp = await self._request("POST", "servers", json=server.model_dump())
        return ServerDTO.model_validate(resp.json())

    async def update_server(self, server_id: int, server: ServerUpdate) -> ServerDTO:
        resp = await self._request(
            "PATCH", f"servers/{server_id}", json=server.model_dump(exclude_unset=True)
        )
        return ServerDTO.model_validate(resp.json())

    # --- API Keys ---

    async def get_api_key(self, service: str) -> APIKeyDTO | None:
        try:
            resp = await self._request("GET", f"api-keys/{service}")
            return APIKeyDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


api_client = SchedulerAPIClient()
