"""Service-specific API client for LangGraph."""

from __future__ import annotations

import os
from http import HTTPStatus
from typing import Any

import httpx

from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.server import ServerDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO
from shared.contracts.dto.user import UserDTO
from shared.log_config.correlation import get_correlation_id
from src.config.settings import get_settings


class LanggraphAPIClient:
    """HTTP client for LangGraph's required API endpoints."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.api_base_url.rstrip("/")
        if self.base_url.endswith("/api"):
            raise RuntimeError("API_BASE_URL must not include /api")
        self._internal_api_key = os.environ["INTERNAL_API_KEY"]
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
        headers = kwargs.pop("headers", None) or {}
        headers["X-Internal-Key"] = self._internal_api_key
        correlation_id = get_correlation_id()
        if correlation_id:
            headers.setdefault("X-Correlation-ID", correlation_id)
        kwargs["headers"] = headers
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

    async def list_projects(self, *, telegram_id: int | None = None) -> list[ProjectDTO]:
        headers = {"X-Telegram-ID": str(telegram_id)} if telegram_id else None
        resp = await self._request("GET", "projects/", headers=headers)
        return [ProjectDTO.model_validate(p) for p in resp.json()]

    async def list_servers(self, is_managed: bool | None = None) -> list[ServerDTO]:
        params = {}
        if is_managed is not None:
            params["is_managed"] = "true" if is_managed else "false"
        resp = await self._request("GET", "servers/", params=params)
        return [ServerDTO.model_validate(s) for s in resp.json()]

    async def get_server(self, server_handle: str) -> ServerDTO:
        resp = await self._request("GET", f"servers/{server_handle}")
        return ServerDTO.model_validate(resp.json())

    async def get_server_ssh_key(self, server_handle: str) -> str | None:
        """Get decrypted SSH private key for a server. Returns None if not stored."""
        try:
            data = await self._get_json(f"servers/{server_handle}/ssh-key")
            return data.get("ssh_key")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise

    async def get_user_by_telegram(self, telegram_id: int) -> UserDTO:
        resp = await self._request("GET", f"users/by-telegram/{telegram_id}")
        return UserDTO.model_validate(resp.json())

    async def update_server(self, server_handle: str, payload: dict) -> dict:
        return await self._patch_json(f"servers/{server_handle}", json=payload)

    async def get_server_services(self, server_handle: str) -> list[dict]:
        return await self._get_json(f"servers/{server_handle}/applications")

    async def list_server_ports(self, server_handle: str) -> list[dict]:
        return await self._get_json(f"servers/{server_handle}/ports")

    async def allocate_server_port(self, server_handle: str, payload: dict) -> dict:
        return await self._post_json(f"servers/{server_handle}/ports", json=payload)

    async def allocate_next_port(self, server_handle: str, payload: dict) -> dict:
        return await self._post_json(f"servers/{server_handle}/ports/allocate-next", json=payload)

    async def create_service_deployment(self, payload: dict) -> dict:
        return await self._post_json("service-deployments/", json=payload)

    async def create_deployment(self, payload: dict) -> dict:
        return await self._post_json("service-deployments/", json=payload)

    async def update_deployment(self, deployment_id: int, payload: dict) -> dict:
        return await self._patch_json(f"service-deployments/{deployment_id}", json=payload)

    # --- Applications ---

    async def list_applications(self, params: dict | None = None) -> list[dict]:
        return await self._get_json("applications/", params=params or {})

    async def get_application(self, application_id: int) -> ApplicationDTO:
        data = await self._get_json(f"applications/{application_id}")
        return ApplicationDTO.model_validate(data)

    async def create_application(self, payload: dict) -> dict:
        return await self._post_json("applications/", json=payload)

    async def update_application(self, application_id: int, payload: dict) -> dict:
        return await self._patch_json(f"applications/{application_id}", json=payload)

    async def get_or_create_application(
        self, repo_id: str, server_handle: str, service_name: str
    ) -> dict:
        """Find existing application or create a new one."""
        apps = await self.list_applications({"repo_id": repo_id, "server_handle": server_handle})
        if apps:
            return apps[0]
        return await self.create_application(
            {
                "repo_id": repo_id,
                "server_handle": server_handle,
                "service_name": service_name,
                "status": "not_deployed",
            }
        )

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

    # --- Architect: story/task methods ---

    async def get_story(self, story_id: str) -> StoryDTO:
        resp = await self._request("GET", f"stories/{story_id}")
        return StoryDTO.model_validate(resp.json())

    async def get_tasks_by_story(self, story_id: str) -> list[TaskDTO]:
        resp = await self._request("GET", "tasks/", params={"story_id": story_id})
        return [TaskDTO.model_validate(t) for t in resp.json()]

    async def get_task_events(self, task_id: str) -> list[TaskEventDTO]:
        resp = await self._request("GET", f"tasks/{task_id}/events")
        return [TaskEventDTO.model_validate(e) for e in resp.json()]

    async def create_task(self, task_data: dict) -> TaskDTO:
        resp = await self._request("POST", "tasks/", json=task_data)
        return TaskDTO.model_validate(resp.json())

    async def transition_story(self, story_id: str, action: str) -> StoryDTO:
        resp = await self._request("POST", f"stories/{story_id}/{action}")
        return StoryDTO.model_validate(resp.json())

    # --- Phase 4: Project methods ---

    async def get_project(
        self, project_id: str, *, telegram_id: int | None = None
    ) -> ProjectDTO | None:
        """Get a single project by ID."""
        headers = {"X-Telegram-ID": str(telegram_id)} if telegram_id else None
        try:
            resp = await self._request("GET", f"projects/{project_id}", headers=headers)
            return ProjectDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise

    async def merge_secrets(
        self, project_id: str, secrets: dict[str, str], env_hints: dict[str, str] | None = None
    ) -> dict:
        """Atomically merge secrets into project config (server-side locking)."""
        payload: dict = {"secrets": secrets}
        if env_hints:
            payload["env_hints"] = env_hints
        return await self._post_json(f"projects/{project_id}/config/secrets", json=payload)

    async def get_project_repositories(self, project_id: str) -> list[RepositoryDTO]:
        """Get repositories for a project."""
        resp = await self._request("GET", "repositories/", params={"project_id": project_id})
        return [RepositoryDTO.model_validate(r) for r in resp.json()]

    async def get_primary_repository(self, project_id: str) -> RepositoryDTO | None:
        """Get the primary repository for a project."""
        repos = await self.get_project_repositories(project_id)
        for repo in repos:
            if repo.role == "primary":
                return repo
        return repos[0] if repos else None

    async def get_repository(self, repo_id: str) -> RepositoryDTO:
        """Get a single repository by ID."""
        resp = await self._request("GET", f"repositories/{repo_id}")
        return RepositoryDTO.model_validate(resp.json())

    async def update_repository(self, repo_id: str, payload: dict) -> RepositoryDTO:
        """PATCH a repository."""
        resp = await self._request("PATCH", f"repositories/{repo_id}", json=payload)
        return RepositoryDTO.model_validate(resp.json())

    # --- Phase 4: Allocation methods ---

    async def get_application_allocations(self, application_id: int) -> list[dict]:
        """Get all port allocations for an application."""
        return await self._get_json("allocations/", params={"application_id": application_id})

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
