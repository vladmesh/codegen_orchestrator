"""Service-specific API client for Scheduler."""

from __future__ import annotations

import os

import httpx

from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.incident import IncidentDTO
from shared.contracts.dto.project import ProjectDTO, ProjectUpdate
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunDTO
from shared.contracts.dto.server import ServerCreate, ServerDTO, ServerUpdate
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO
from shared.contracts.dto.user import UserDTO
from shared.log_config.correlation import get_correlation_id
from src.config import get_settings


class SchedulerAPIClient:
    """HTTP client for scheduler-required API endpoints."""

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

    async def get_repository_by_provider_id(self, provider_repo_id: int) -> RepositoryDTO | None:
        try:
            resp = await self._request("GET", f"repositories/by-provider-id/{provider_repo_id}")
            return RepositoryDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    async def get_repositories(self, project_id: str | None = None) -> list[RepositoryDTO]:
        params = {}
        if project_id:
            params["project_id"] = project_id
        resp = await self._request("GET", "repositories/", params=params)
        return [RepositoryDTO.model_validate(r) for r in resp.json()]

    async def get_primary_repository(self, project_id: str) -> RepositoryDTO | None:
        """Get the primary repository for a project."""
        repos = await self.get_repositories(project_id=project_id)
        for repo in repos:
            if repo.role == "primary":
                return repo
        return repos[0] if repos else None

    async def update_repository(self, repo_id: str, fields: dict) -> RepositoryDTO:
        resp = await self._request("PATCH", f"repositories/{repo_id}", json=fields)
        return RepositoryDTO.model_validate(resp.json())

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

    async def create_run(self, run_data: dict) -> RunDTO:
        resp = await self._request("POST", "runs/", json=run_data)
        return RunDTO.model_validate(resp.json())

    async def get_run(self, run_id: str) -> RunDTO:
        resp = await self._request("GET", f"runs/{run_id}")
        return RunDTO.model_validate(resp.json())

    async def get_latest_run_by_story(
        self, story_id: str, run_type: str | None = None
    ) -> RunDTO | None:
        """Return the newest run for a story, validating only that run.

        The runs endpoint returns the story's runs newest-first. Routing only
        cares about the latest one, so we validate `rows[0]` alone — an older,
        legacy/corrupt run must not fail a story whose current run is valid.
        """
        params: dict[str, str] = {"story_id": story_id}
        if run_type:
            params["run_type"] = run_type
        resp = await self._request("GET", "runs/", params=params)
        rows = resp.json()
        if not rows:
            return None
        return RunDTO.model_validate(rows[0])

    # --- Stories ---

    async def get_story(self, story_id: str) -> StoryDTO:
        resp = await self._request("GET", f"stories/{story_id}")
        return StoryDTO.model_validate(resp.json())

    async def get_stories_by_status(self, status: str) -> list[StoryDTO]:
        resp = await self._request("GET", "stories/", params={"status": status})
        return [StoryDTO.model_validate(s) for s in resp.json()]

    async def get_stories_by_project(self, project_id: str) -> list[StoryDTO]:
        resp = await self._request("GET", "stories/", params={"project_id": project_id})
        return [StoryDTO.model_validate(s) for s in resp.json()]

    async def fail_story(self, story_id: str) -> StoryDTO:
        """Transition story to failed status."""
        resp = await self._request("POST", f"stories/{story_id}/fail", json={"actor": "supervisor"})
        return StoryDTO.model_validate(resp.json())

    async def transition_story(self, story_id: str, action: str) -> StoryDTO:
        """Transition story status. action: 'start', 'complete', 'archive'."""
        resp = await self._request(
            "POST", f"stories/{story_id}/{action}", json={"actor": "architect"}
        )
        return StoryDTO.model_validate(resp.json())

    async def update_story(self, story_id: str, data: dict) -> StoryDTO:
        """Patch story fields (e.g. pr_number)."""
        resp = await self._request("PATCH", f"stories/{story_id}", json=data)
        return StoryDTO.model_validate(resp.json())

    # --- Tasks ---

    async def get_tasks_by_status(self, status: str) -> list[TaskDTO]:
        resp = await self._request("GET", "tasks/", params={"status": status})
        return [TaskDTO.model_validate(t) for t in resp.json()]

    async def get_tasks_by_story(self, story_id: str) -> list[TaskDTO]:
        resp = await self._request("GET", "tasks/", params={"story_id": story_id})
        return [TaskDTO.model_validate(t) for t in resp.json()]

    async def get_tasks_by_project_and_status(
        self,
        project_id: str,
        status: str,
    ) -> list[TaskDTO]:
        resp = await self._request(
            "GET",
            "tasks/",
            params={"project_id": project_id, "status": status},
        )
        return [TaskDTO.model_validate(t) for t in resp.json()]

    async def create_task(self, task_data: dict) -> TaskDTO:
        resp = await self._request("POST", "tasks/", json=task_data)
        return TaskDTO.model_validate(resp.json())

    async def update_task(self, task_id: str, data: dict) -> TaskDTO:
        resp = await self._request("PATCH", f"tasks/{task_id}", json=data)
        return TaskDTO.model_validate(resp.json())

    async def get_task(self, task_id: str) -> TaskDTO:
        resp = await self._request("GET", f"tasks/{task_id}")
        return TaskDTO.model_validate(resp.json())

    async def transition_task(
        self, task_id: str, to_status: str, actor: str = "architect"
    ) -> TaskDTO:
        resp = await self._request(
            "POST",
            f"tasks/{task_id}/transition",
            params={"to_status": to_status},
            json={"actor": actor},
        )
        return TaskDTO.model_validate(resp.json())

    async def create_task_event(self, task_id: str, event: dict) -> TaskEventDTO:
        resp = await self._request("POST", f"tasks/{task_id}/events", json=event)
        return TaskEventDTO.model_validate(resp.json())

    async def get_task_events(self, task_id: str) -> list[TaskEventDTO]:
        resp = await self._request("GET", f"tasks/{task_id}/events")
        return [TaskEventDTO.model_validate(e) for e in resp.json()]

    # --- Incidents ---

    async def create_incident(
        self,
        server_handle: str,
        incident_type: str,
        details: dict,
        affected_services: list[str] | None = None,
    ) -> IncidentDTO:
        resp = await self._request(
            "POST",
            "incidents/",
            json={
                "server_handle": server_handle,
                "incident_type": incident_type,
                "details": details,
                "affected_services": affected_services or [],
            },
        )
        return IncidentDTO.model_validate(resp.json())

    async def get_active_incidents(
        self, server_handle: str, incident_type: str
    ) -> list[IncidentDTO]:
        resp = await self._request(
            "GET",
            "incidents/",
            params={
                "server_handle": server_handle,
                "incident_type": incident_type,
                "status": "detected",
            },
        )
        return [IncidentDTO.model_validate(i) for i in resp.json()]

    async def resolve_incident(self, incident_id: int) -> IncidentDTO:
        from datetime import UTC, datetime

        resp = await self._request(
            "PATCH",
            f"incidents/{incident_id}",
            json={
                "status": "resolved",
                "resolved_at": datetime.now(UTC).isoformat(),
            },
        )
        return IncidentDTO.model_validate(resp.json())

    # --- Metrics History ---

    async def create_metrics_history(self, server_handle: str, metrics: dict) -> dict:
        resp = await self._request(
            "POST",
            f"servers/{server_handle}/metrics-history",
            json={"metrics": metrics},
        )
        return resp.json()

    async def delete_old_metrics_history(self, retention_hours: int = 168) -> dict:
        resp = await self._request(
            "DELETE",
            "servers/metrics-history",
            params={"retention_hours": retention_hours},
        )
        return resp.json()

    # --- Applications ---

    async def get_applications(
        self,
        server_handle: str | None = None,
        status: str | None = None,
    ) -> list[ApplicationDTO]:
        """Get applications with optional filtering."""
        params: dict = {}
        if server_handle:
            params["server_handle"] = server_handle
        if status:
            params["status"] = status
        resp = await self._request("GET", "applications/", params=params)
        return [ApplicationDTO.model_validate(a) for a in resp.json()]

    async def update_application(self, app_id: int, fields: dict) -> ApplicationDTO:
        """Update application fields (status, health metrics, etc.)."""
        resp = await self._request("PATCH", f"applications/{app_id}", json=fields)
        return ApplicationDTO.model_validate(resp.json())

    async def create_app_health_history(self, app_id: int, metrics: dict) -> dict:
        """Append a health history snapshot for an application."""
        resp = await self._request(
            "POST",
            f"applications/{app_id}/health-history",
            json={"metrics": metrics},
        )
        return resp.json()

    async def delete_old_app_health_history(self, retention_hours: int = 168) -> dict:
        """Delete application health history older than retention period."""
        resp = await self._request(
            "DELETE",
            "applications/health-history",
            params={"retention_hours": retention_hours},
        )
        return resp.json()

    async def get_applications_by_project(self, project_id: str) -> list[ApplicationDTO]:
        """Get applications for a project (via its repositories)."""
        repos = await self.get_repositories(project_id)
        if not repos:
            return []
        results = []
        for repo in repos:
            resp = await self._request("GET", "applications/", params={"repo_id": repo.id})
            results.extend(ApplicationDTO.model_validate(a) for a in resp.json())
        return results

    # --- Users ---

    async def get_user(self, user_id: int) -> UserDTO | None:
        try:
            resp = await self._request("GET", f"users/{user_id}")
            return UserDTO.model_validate(resp.json())
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

    # --- Analytics ---

    async def upsert_analytics_hourly(self, data: dict) -> dict:
        """Upsert an hourly analytics row."""
        resp = await self._request("POST", "analytics/hourly", json=data)
        return resp.json()

    async def upsert_analytics_daily(self, data: dict) -> dict:
        """Upsert a daily analytics row."""
        resp = await self._request("POST", "analytics/daily", json=data)
        return resp.json()

    async def upsert_known_users(self, project_id: str, users: list[dict]) -> dict:
        """Batch upsert known users for a project."""
        resp = await self._request(
            "POST",
            "analytics/known-users",
            json={"project_id": project_id, "users": users},
        )
        return resp.json()

    async def get_known_users(self, project_id: str) -> list[dict]:
        """Get known users for a project."""
        resp = await self._request(
            "GET",
            "analytics/known-users",
            params={"project_id": project_id},
        )
        return resp.json()

    async def get_analytics_hourly(
        self,
        project_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        """Get hourly analytics for a project."""
        params: dict[str, str] = {"project_id": project_id}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        resp = await self._request("GET", "analytics/hourly", params=params)
        return resp.json()

    async def delete_old_hourly(self, days: int) -> dict:
        """Delete hourly analytics older than N days."""
        resp = await self._request(
            "DELETE",
            "analytics/hourly",
            params={"older_than_days": days},
        )
        return resp.json()

    async def delete_old_daily(self, days: int) -> dict:
        """Delete daily analytics older than N days."""
        resp = await self._request(
            "DELETE",
            "analytics/daily",
            params={"older_than_days": days},
        )
        return resp.json()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


api_client = SchedulerAPIClient()
