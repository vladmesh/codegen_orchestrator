"""Service-specific API client for LangGraph."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.application import DEFAULT_APPLICATION_RESERVED_RAM_MB, ApplicationDTO
from shared.contracts.dto.deploy_dispatch import DeployDispatchClaim, DeployRunStart
from shared.contracts.dto.incident import IncidentCreate, IncidentDTO, IncidentType
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunDTO
from shared.contracts.dto.server import ServerDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO
from shared.contracts.dto.telegram import BotLiveness
from shared.contracts.dto.user import UserDTO
from src.config.settings import get_settings


def bot_liveness_path(project_id: str) -> str:
    """The internal surface that answers "is this project's bot live?".

    Named here rather than inlined because a QA blocker has to say by which path
    the answer was obtained: the token stays in the API, and this is the whole of
    what the QA runtime asks it.
    """
    return f"projects/{project_id}/telegram/liveness"


class LanggraphAPIClient(InternalAPIClient):
    """HTTP client for LangGraph's required API endpoints."""

    def __init__(self) -> None:
        super().__init__(get_settings().api_base_url)

    async def get(self, path: str, headers: dict | None = None, **kwargs) -> dict | list:
        resp = await self.request("GET", path, headers=headers, **kwargs)
        return resp.json()

    async def post(self, path: str, headers: dict | None = None, **kwargs) -> dict:
        resp = await self.request("POST", path, headers=headers, **kwargs)
        return resp.json()

    async def patch(self, path: str, headers: dict | None = None, **kwargs) -> dict:
        resp = await self.request("PATCH", path, headers=headers, **kwargs)
        return resp.json()

    async def delete(self, path: str, headers: dict | None = None, **kwargs) -> dict | None:
        resp = await self.request("DELETE", path, headers=headers, **kwargs)
        if resp.status_code == httpx.codes.NO_CONTENT:
            return None
        return resp.json()

    async def _get_json(self, path: str, **kwargs) -> dict | list:
        resp = await self.request("GET", path, **kwargs)
        return resp.json()

    async def _post_json(self, path: str, **kwargs) -> dict:
        resp = await self.request("POST", path, **kwargs)
        return resp.json()

    async def _patch_json(self, path: str, **kwargs) -> dict:
        resp = await self.request("PATCH", path, **kwargs)
        return resp.json()

    async def get_agent_config(self, agent_id: str) -> dict[str, Any]:
        return await self._get_json(f"agent-configs/{agent_id}")

    async def list_projects(self, *, telegram_id: int | None = None) -> list[ProjectDTO]:
        headers = {"X-Telegram-ID": str(telegram_id)} if telegram_id else None
        resp = await self.request("GET", "projects/", headers=headers)
        return [ProjectDTO.model_validate(p) for p in resp.json()]

    async def list_servers(self, is_managed: bool | None = None) -> list[ServerDTO]:
        params = {}
        if is_managed is not None:
            params["is_managed"] = "true" if is_managed else "false"
        resp = await self.request("GET", "servers/", params=params)
        return [ServerDTO.model_validate(s) for s in resp.json()]

    async def get_server(self, server_handle: str) -> ServerDTO:
        resp = await self.request("GET", f"servers/{server_handle}")
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
        resp = await self.request("GET", f"users/by-telegram/{telegram_id}")
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

    # --- Runs ---

    async def get_run(self, run_id: str) -> RunDTO:
        data = await self._get_json(f"runs/{run_id}")
        return RunDTO.model_validate(data)

    async def list_runs_holding_qa_ssh_grant(
        self, *, limit: int, after: RunDTO | None = None
    ) -> list[RunDTO]:
        """One page of the runs whose QA SSH grant is not proven released.

        Selected by the state of the record and ordered oldest first, so the
        sweep's work is every open grant rather than the ones belonging to a
        recent run. The page bounds the response; the caller keeps asking from
        the last record it handled until a page comes back short.

        `after` is that record, and the cursor is its `(created_at, id)`. It is
        a position in the order, not a count of rows, so records the caller has
        already released — and which therefore leave the selection — cannot
        move the unhandled ones out of reach behind it.
        """
        params: dict = {"limit": limit}
        if after is not None:
            params["after_created_at"] = after.created_at.isoformat()
            params["after_id"] = after.id
        data = await self._get_json("runs/qa-ssh-grants/held", params=params)
        return [RunDTO.model_validate(run) for run in data]

    async def start_run(self, run_id: str) -> DeployRunStart:
        """Take the run to RUNNING as one locked decision, or learn it is over."""
        data = await self._post_json(f"runs/{run_id}/start")
        return DeployRunStart.model_validate(data)

    async def claim_deploy_dispatch(self, run_id: str) -> DeployDispatchClaim:
        """Take the dispatch boundary before starting work outside the system."""
        data = await self._post_json(f"runs/{run_id}/dispatch-claim")
        return DeployDispatchClaim.model_validate(data)

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
        self,
        repo_id: str,
        server_handle: str,
        service_name: str,
        reserved_ram_mb: int = DEFAULT_APPLICATION_RESERVED_RAM_MB,
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
                "reserved_ram_mb": reserved_ram_mb,
                "status": "not_deployed",
            }
        )

    async def query_rag(self, payload: dict) -> dict:
        return await self._post_json("rag/query", json=payload)

    async def create_incident(self, payload: dict) -> dict:
        return await self._post_json("incidents/", json=payload)

    async def record_provisioning_failure(self, incident: IncidentCreate) -> IncidentDTO:
        """Record a provisioning failure in its one active episode for that server.

        The endpoint upserts, so a fact rediscovered on every QA run of a broken
        host stays one journal entry with a rising attempt count instead of a row
        per run.
        """
        if incident.incident_type is not IncidentType.PROVISIONING_FAILED:
            raise ValueError("record_provisioning_failure requires provisioning_failed")
        data = await self._post_json(
            "incidents/provisioning-failure", json=incident.model_dump(mode="json")
        )
        return IncidentDTO.model_validate(data)

    async def list_active_incidents(self) -> list[IncidentDTO]:
        """Return detected and recovering incidents, typed for admission checks."""
        incidents = await self._get_json("incidents/active")
        return [IncidentDTO.model_validate(incident) for incident in incidents]

    async def list_incidents(self, params: dict) -> list[dict]:
        return await self._get_json("incidents/", params=params)

    async def update_incident(self, incident_id: int, payload: dict) -> dict:
        return await self._patch_json(f"incidents/{incident_id}", json=payload)

    # --- Architect: story/task methods ---

    async def get_story(self, story_id: str) -> StoryDTO:
        resp = await self.request("GET", f"stories/{story_id}")
        return StoryDTO.model_validate(resp.json())

    async def get_tasks_by_story(self, story_id: str) -> list[TaskDTO]:
        resp = await self.request("GET", "tasks/", params={"story_id": story_id})
        return [TaskDTO.model_validate(t) for t in resp.json()]

    async def get_task_events(self, task_id: str) -> list[TaskEventDTO]:
        resp = await self.request("GET", f"tasks/{task_id}/events")
        return [TaskEventDTO.model_validate(e) for e in resp.json()]

    async def create_task(self, task_data: dict) -> TaskDTO:
        resp = await self.request("POST", "tasks/", json=task_data)
        return TaskDTO.model_validate(resp.json())

    async def transition_story(self, story_id: str, action: str) -> StoryDTO:
        resp = await self.request("POST", f"stories/{story_id}/{action}")
        return StoryDTO.model_validate(resp.json())

    # --- Phase 4: Project methods ---

    async def get_project(
        self, project_id: str, *, telegram_id: int | None = None
    ) -> ProjectDTO | None:
        """Get a single project by ID."""
        headers = {"X-Telegram-ID": str(telegram_id)} if telegram_id else None
        try:
            resp = await self.request("GET", f"projects/{project_id}", headers=headers)
            return ProjectDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise

    async def get_bot_liveness(self, project_id: str) -> BotLiveness:
        """Ask whether this project's bot answers `getMe` right now.

        The API holds the token and asks Telegram on this caller's behalf, so a
        QA run learns that the bot is live without the token entering its
        runtime — see `bot_liveness_path` for the surface this is the client of.
        """
        return BotLiveness.model_validate(await self._get_json(bot_liveness_path(project_id)))

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
        resp = await self.request("GET", "repositories/", params={"project_id": project_id})
        return [RepositoryDTO.model_validate(r) for r in resp.json()]

    async def get_primary_repository(self, project_id: str) -> RepositoryDTO | None:
        """Get the primary repository for a project."""
        repos = await self.get_project_repositories(project_id)
        for repo in repos:
            if repo.role == "primary":
                return repo
        return repos[0] if repos else None

    async def update_repository(self, repo_id: str, payload: dict) -> RepositoryDTO:
        """PATCH a repository."""
        resp = await self.request("PATCH", f"repositories/{repo_id}", json=payload)
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

    async def release_allocation(self, allocation_id: int) -> None:
        """Release a port allocation."""
        await self.delete(f"allocations/{allocation_id}")


api_client = LanggraphAPIClient()
