"""Service-specific API client for Scheduler."""

from __future__ import annotations

import httpx

from shared.contracts.dto.project import ProjectDTO, ProjectUpdate
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

    async def get_projects(self) -> list[ProjectDTO]:
        resp = await self._request("GET", "projects")
        return [ProjectDTO.model_validate(p) for p in resp.json()]

    async def get_project(self, project_id: str) -> ProjectDTO | None:
        try:
            resp = await self._request("GET", f"projects/{project_id}")
            return ProjectDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    async def update_project(self, project_id: str, project: ProjectUpdate) -> ProjectDTO:
        resp = await self._request(
            "PATCH", f"projects/{project_id}", json=project.model_dump(exclude_unset=True)
        )
        return ProjectDTO.model_validate(resp.json())

    # --- Repositories ---

    async def get_repository_by_provider_id(self, provider_repo_id: int) -> dict | None:
        try:
            resp = await self._request("GET", f"repositories/by-provider-id/{provider_repo_id}")
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    async def get_repositories(self, project_id: str | None = None) -> list[dict]:
        params = {}
        if project_id:
            params["project_id"] = project_id
        resp = await self._request("GET", "repositories/", params=params)
        return resp.json()

    # --- Servers ---

    async def get_servers(self) -> list[ServerDTO]:
        resp = await self._request("GET", "servers")
        return [ServerDTO.model_validate(s) for s in resp.json()]

    async def create_server(self, server: ServerCreate) -> ServerDTO:
        resp = await self._request("POST", "servers", json=server.model_dump())
        return ServerDTO.model_validate(resp.json())

    async def update_server(self, server_id: str, server: ServerUpdate) -> ServerDTO:
        resp = await self._request(
            "PATCH", f"servers/{server_id}", json=server.model_dump(mode="json", exclude_unset=True)
        )
        return ServerDTO.model_validate(resp.json())

    # --- Runs ---

    async def create_run(self, run_data: dict) -> dict:
        resp = await self._request("POST", "runs/", json=run_data)
        return resp.json()

    # --- Stories ---

    async def get_story(self, story_id: str) -> dict:
        resp = await self._request("GET", f"stories/{story_id}")
        return resp.json()

    async def get_stories_by_status(self, status: str) -> list[dict]:
        resp = await self._request("GET", "stories/", params={"status": status})
        return resp.json()

    async def get_stories_by_project(self, project_id: str) -> list[dict]:
        resp = await self._request("GET", "stories/", params={"project_id": project_id})
        return resp.json()

    async def fail_story(self, story_id: str) -> dict:
        """Transition story to failed status."""
        resp = await self._request("POST", f"stories/{story_id}/fail", json={"actor": "supervisor"})
        return resp.json()

    async def transition_story(self, story_id: str, action: str) -> dict:
        """Transition story status. action: 'start', 'complete', 'archive'."""
        resp = await self._request(
            "POST", f"stories/{story_id}/{action}", json={"actor": "architect"}
        )
        return resp.json()

    # --- Tasks ---

    async def get_tasks_by_status(self, status: str) -> list[dict]:
        resp = await self._request("GET", "tasks/", params={"status": status})
        return resp.json()

    async def get_tasks_by_story(self, story_id: str) -> list[dict]:
        resp = await self._request("GET", "tasks/", params={"story_id": story_id})
        return resp.json()

    async def create_task(self, task_data: dict) -> dict:
        resp = await self._request("POST", "tasks/", json=task_data)
        return resp.json()

    async def update_task(self, task_id: str, data: dict) -> dict:
        resp = await self._request("PATCH", f"tasks/{task_id}", json=data)
        return resp.json()

    async def get_task(self, task_id: str) -> dict:
        resp = await self._request("GET", f"tasks/{task_id}")
        return resp.json()

    async def transition_task(self, task_id: str, to_status: str, actor: str = "architect") -> dict:
        resp = await self._request(
            "POST",
            f"tasks/{task_id}/transition",
            params={"to_status": to_status},
            json={"actor": actor},
        )
        return resp.json()

    async def create_task_event(self, task_id: str, event: dict) -> dict:
        resp = await self._request("POST", f"tasks/{task_id}/events", json=event)
        return resp.json()

    async def get_task_events(self, task_id: str) -> list[dict]:
        resp = await self._request("GET", f"tasks/{task_id}/events")
        return resp.json()

    # --- Users ---

    async def get_user(self, user_id: int) -> dict | None:
        try:
            resp = await self._request("GET", f"users/{user_id}")
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    # --- API Keys ---

    async def get_api_key(self, service: str) -> dict | None:
        try:
            resp = await self._request("GET", f"api-keys/{service}")
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


api_client = SchedulerAPIClient()
