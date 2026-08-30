"""Service-specific API client for Scheduler."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.deploy_dispatch import (
    DeployDispatchSupersede,
    DeployDispatchWithdrawal,
)
from shared.contracts.dto.engineering_budget_policy import (
    EngineeringBudgetAdmissionCommand,
    EngineeringBudgetAdmissionRead,
)
from shared.contracts.dto.incident import IncidentDTO
from shared.contracts.dto.owner_notification import OwnerNotification
from shared.contracts.dto.project import ProjectDTO, ProjectUpdate
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunDTO
from shared.contracts.dto.run_result import QARunResult
from shared.contracts.dto.server import ServerCreate, ServerDTO, ServerStatus, ServerUpdate
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantCreate,
    TemporaryAccessGrantDTO,
    TemporaryAccessGrantUpdate,
    TemporaryAccessObservation,
)
from shared.contracts.dto.user import UserDTO
from shared.contracts.dto.users_grant import GrantIntentLifecycleResult
from shared.contracts.dto.work_admission import PaidRunStartCommand, PaidRunStartRead
from src.config import get_settings


class StoryOwnerNotificationRead(BaseModel):
    """Internal recovery view of a story-backed completion notification."""

    id: str
    owner_notification: OwnerNotification


class SchedulerAPIClient(InternalAPIClient):
    """HTTP client for scheduler-required API endpoints."""

    def __init__(self) -> None:
        super().__init__(get_settings().api_base_url)

    async def ingest_rag(self, body: bytes, headers: dict) -> dict:
        resp = await self.request("POST", "rag/ingest", content=body, headers=headers)
        return resp.json()

    # --- Projects ---

    async def get_projects(self) -> list[ProjectDTO]:
        resp = await self.request("GET", "projects")
        return [ProjectDTO.model_validate(p) for p in resp.json()]

    async def get_project(self, project_id: str) -> ProjectDTO | None:
        try:
            resp = await self.request("GET", f"projects/{project_id}")
            return ProjectDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    async def update_project(self, project_id: str, project: ProjectUpdate) -> ProjectDTO:
        resp = await self.request(
            "PATCH", f"projects/{project_id}", json=project.model_dump(exclude_unset=True)
        )
        return ProjectDTO.model_validate(resp.json())

    async def list_project_secret_keys(self, project_id: str) -> list[str]:
        """Return the names of secrets stored on a project (never the values)."""
        resp = await self.request("GET", f"projects/{project_id}/config/secrets/keys")
        return resp.json()["keys"]

    # --- Repositories ---

    async def get_repository_by_provider_id(self, provider_repo_id: int) -> RepositoryDTO | None:
        try:
            resp = await self.request("GET", f"repositories/by-provider-id/{provider_repo_id}")
            return RepositoryDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    async def get_repositories(self, project_id: str | None = None) -> list[RepositoryDTO]:
        params = {}
        if project_id:
            params["project_id"] = project_id
        resp = await self.request("GET", "repositories/", params=params)
        return [RepositoryDTO.model_validate(r) for r in resp.json()]

    async def get_primary_repository(self, project_id: str) -> RepositoryDTO | None:
        """Get the primary repository for a project."""
        repos = await self.get_repositories(project_id=project_id)
        for repo in repos:
            if repo.role == "primary":
                return repo
        return repos[0] if repos else None

    async def update_repository(self, repo_id: str, fields: dict) -> RepositoryDTO:
        resp = await self.request("PATCH", f"repositories/{repo_id}", json=fields)
        return RepositoryDTO.model_validate(resp.json())

    # --- Servers ---

    async def get_servers(self, status: ServerStatus | None = None) -> list[ServerDTO]:
        params = {"status": status.value} if status else {}
        resp = await self.request("GET", "servers/", params=params)
        return [ServerDTO.model_validate(s) for s in resp.json()]

    async def get_server(self, server_handle: str) -> ServerDTO:
        resp = await self.request("GET", f"servers/{server_handle}")
        return ServerDTO.model_validate(resp.json())

    async def create_server(self, server: ServerCreate) -> ServerDTO:
        resp = await self.request("POST", "servers", json=server.model_dump())
        return ServerDTO.model_validate(resp.json())

    async def update_server(self, server_id: str, server: ServerUpdate) -> ServerDTO:
        resp = await self.request(
            "PATCH", f"servers/{server_id}", json=server.model_dump(mode="json", exclude_unset=True)
        )
        return ServerDTO.model_validate(resp.json())

    # --- Runs ---

    async def admit_engineering_budget(
        self, command: EngineeringBudgetAdmissionCommand
    ) -> EngineeringBudgetAdmissionRead:
        resp = await self.request(
            "POST", "engineering-budget-policies/admissions", json=command.model_dump(mode="json")
        )
        return EngineeringBudgetAdmissionRead.model_validate(resp.json())

    async def start_paid_run(self, command: PaidRunStartCommand) -> PaidRunStartRead:
        resp = await self.request(
            "POST", "work-admission/paid-runs", json=command.model_dump(mode="json")
        )
        return PaidRunStartRead.model_validate(resp.json())

    async def release_engineering_budget_admission(self, attempt_id: str) -> None:
        await self.request("POST", f"engineering-budget-policies/admissions/{attempt_id}/release")

    async def abort_paid_run_pre_handoff(self, run_id: str, reason: str) -> None:
        await self.request(
            "POST", f"work-admission/paid-runs/{run_id}/abort-pre-handoff", json={"reason": reason}
        )

    async def create_run(self, run_data: dict) -> RunDTO:
        resp = await self.request("POST", "runs/", json=run_data)
        return RunDTO.model_validate(resp.json())

    async def resume_initial_owner_grant(
        self, project_id: str, *, story_id: str, head_sha: str
    ) -> GrantIntentLifecycleResult:
        resp = await self.request(
            "POST",
            f"projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "story_id": story_id, "head_sha": head_sha},
        )
        return GrantIntentLifecycleResult.model_validate(resp.json())

    async def create_run_if_absent(self, run_data: dict) -> RunDTO:
        """Create a run, or return the one already carrying this id.

        The handoff names its QA run after the deploy run it came from, so a tick
        repeating a handoff that died part-way through has to land on the same
        run. A second run for the same deploy would be a second QA attempt, and
        the story would then have two runs disagreeing about its outcome.
        """
        existing = await self.get_run_if_missing_returns_none(run_data["id"])
        if existing is not None:
            return existing
        return await self.create_run(run_data)

    async def get_run(self, run_id: str) -> RunDTO:
        resp = await self.request("GET", f"runs/{run_id}")
        return RunDTO.model_validate(resp.json())

    async def list_runs(
        self, *, task_id: str, run_type: str, status: str | None = None
    ) -> list[RunDTO]:
        """List runs of a task filtered by type, newest first; status is optional."""
        params = {"task_id": task_id, "run_type": run_type}
        if status is not None:
            params["status"] = status
        resp = await self.request("GET", "runs/", params=params)
        return [RunDTO.model_validate(r) for r in resp.json()]

    async def list_runs_owing_owner_notification(self, *, limit: int) -> list[RunDTO]:
        """One page of the runs whose owner has not been told their story ended.

        Selected by the state of the record and ordered oldest first, so the
        recovery sweep's work is every message still owed rather than the ones
        belonging to a story that happens to still be in a status it scans — a
        terminal transition takes the story out of every such status.
        """
        resp = await self.request("GET", "runs/owner-notifications/owed", params={"limit": limit})
        return [RunDTO.model_validate(row) for row in resp.json()]

    async def list_stories_owing_owner_notification(
        self, *, limit: int
    ) -> list[StoryOwnerNotificationRead]:
        """One page of completed stories whose completion message is still owed."""
        resp = await self.request(
            "GET", "stories/owner-notifications/owed", params={"limit": limit}
        )
        return [StoryOwnerNotificationRead.model_validate(row) for row in resp.json()]

    async def get_story_owner_notification(self, story_id: str) -> OwnerNotification:
        """Read the completion record written by the story transition transaction."""
        resp = await self.request("GET", f"stories/{story_id}/owner-notification")
        return OwnerNotification.model_validate(resp.json())

    async def update_story_owner_notification(self, story_id: str, notification: dict) -> None:
        """Persist one delivery attempt against a story-backed completion record."""
        await self.request("PATCH", f"stories/{story_id}/owner-notification", json=notification)

    async def update_run(self, run_id: str, data: dict) -> None:
        """Patch run fields (status, error_message, result)."""
        await self.request("PATCH", f"runs/{run_id}", json=data)

    async def record_run_outcome_unless_settled(self, run_id: str, data: dict) -> bool:
        """Write a terminal outcome onto a run, unless it already has one.

        Both the sweep and the worker inside a run can decide it is over, and the
        API keeps whichever answer landed first. False here means the run had
        already recorded its own, so the caller's reason is not the one the run
        carries — which is information, not a failure to be raised: the access
        this was about still has to be taken back either way.
        """
        try:
            await self.request("PATCH", f"runs/{run_id}", json=data)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != httpx.codes.CONFLICT:
                raise
            return False
        return True

    async def withdraw_deploy_dispatch(self, run_id: str, reason: str) -> DeployDispatchWithdrawal:
        """Stop a deploy run, and learn whether it got out before the stop landed."""
        resp = await self.request(
            "POST", f"runs/{run_id}/dispatch-withdraw", params={"reason": reason}
        )
        return DeployDispatchWithdrawal.model_validate(resp.json())

    async def supersede_deploy_dispatch(self, run_id: str, reason: str) -> DeployDispatchSupersede:
        """Take a silent dispatch claim back once its holder may no longer act."""
        resp = await self.request(
            "POST", f"runs/{run_id}/dispatch-supersede", params={"reason": reason}
        )
        return DeployDispatchSupersede.model_validate(resp.json())

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
        resp = await self.request("GET", "runs/", params=params)
        rows = resp.json()
        if not rows:
            return None
        return RunDTO.model_validate(rows[0])

    async def get_run_if_missing_returns_none(self, run_id: str) -> RunDTO | None:
        """Read a run, or None when it is gone.

        A run that no longer exists is a real state for the temporary-access
        sweep: whatever it was granted for cannot finish, so the grant is
        settled by revoking it rather than by waiting forever.
        """
        try:
            resp = await self.request("GET", f"runs/{run_id}")
        except httpx.HTTPStatusError as error:
            if error.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise
        return RunDTO.model_validate(resp.json())

    # --- Temporary access grants ---

    async def create_temporary_access_grant(
        self, grant: TemporaryAccessGrantCreate
    ) -> TemporaryAccessGrantDTO:
        """Write the grant down before the access is handed out."""
        resp = await self.request(
            "POST", "temporary-access-grants/", json=grant.model_dump(mode="json")
        )
        return TemporaryAccessGrantDTO.model_validate(resp.json())

    async def list_temporary_access_grants_under_watch(
        self, revoked_after: datetime, slot_audit_before: datetime
    ) -> list[TemporaryAccessGrantDTO]:
        """Every grant the sweep still has to read, whatever process granted it.

        Three sets in one answer. Every grant that may hold access. The ones
        closed since *revoked_after*: a closed grant is not holding access as far
        as the record knows, and the record only knows what was read, so a
        dispatch that was already on its way can write the value back afterwards
        and nobody would see it if the readings stopped when the grant closed.
        And the owner of every closed slot last read before *slot_audit_before*,
        which is the slow level — the value that came back after the fast watch
        ended is found there, later but not never.
        """
        resp = await self.request(
            "GET",
            "temporary-access-grants/",
            params={
                "live": "true",
                "revoked_after": revoked_after.isoformat(),
                "slot_audit_before": slot_audit_before.isoformat(),
            },
        )
        return [TemporaryAccessGrantDTO.model_validate(row) for row in resp.json()]

    async def get_live_temporary_access_grant_for_run(
        self, qa_run_id: str
    ) -> TemporaryAccessGrantDTO | None:
        """The grant that still holds access for this QA run, if there is one.

        The live slot is unique per (project, env key), so a QA run has at most
        one. Two would mean the sweep could revoke one and leave the other.
        """
        resp = await self.request(
            "GET",
            "temporary-access-grants/",
            params={"live": "true", "qa_run_id": qa_run_id},
        )
        rows = resp.json()
        if not rows:
            return None
        if len(rows) > 1:
            raise RuntimeError(f"QA run {qa_run_id} has {len(rows)} live temporary access grants")
        return TemporaryAccessGrantDTO.model_validate(rows[0])

    async def temporary_access_grant_exists_for_run(self, qa_run_id: str) -> bool:
        """Whether any grant was ever recorded for this QA run, live or settled.

        A handoff being recovered asks this, not whether a grant still holds
        access: a grant that has already been revoked means the handoff got
        through, and handing the access out again would restart a lifecycle that
        already ended.
        """
        resp = await self.request(
            "GET", "temporary-access-grants/", params={"qa_run_id": qa_run_id}
        )
        return bool(resp.json())

    async def escalate_temporary_access_grant(
        self,
        grant_id: str,
        *,
        error: str,
        run_error_message: str,
        run_result: QARunResult,
    ) -> TemporaryAccessGrantDTO:
        """Give up on a quiet revoke: the QA run carries the failure, in one write.

        The run that borrowed the identity is where the cleanup incident is
        recorded, so the record of what happened to the access is next to the run
        it was lent to rather than in a log line. It is not what decides the
        story: by the time the sweep runs out of attempts the story has been
        routed on the product verdict QA gave, and a completed one is not
        reopened by anything written here.

        Doing this through the ordinary run patch would be refused, and rightly —
        that path is where a stale worker verdict would overwrite a supervisor's.
        """
        resp = await self.request(
            "POST",
            f"temporary-access-grants/{grant_id}/escalate",
            json={
                "error": error,
                "run_error_message": run_error_message,
                "run_result": run_result.model_dump(mode="json"),
            },
        )
        return TemporaryAccessGrantDTO.model_validate(resp.json())

    async def record_temporary_access_observation(
        self, grant_id: str, observation: TemporaryAccessObservation
    ) -> TemporaryAccessGrantDTO:
        """Hand the record a reading of the running service and read back what it means.

        The caller does not decide whether the grant is closed. It reports what
        the server showed; the record holds the streak of agreeing readings and
        the window they have to span, and answers with the grant as it now
        stands. That way one clear reading cannot end reconciliation, and a
        reading that finds the value again puts the streak back to the start.
        """
        resp = await self.request(
            "POST",
            f"temporary-access-grants/{grant_id}/observation",
            json=observation.model_dump(mode="json"),
        )
        return TemporaryAccessGrantDTO.model_validate(resp.json())

    async def update_temporary_access_grant(
        self, grant_id: str, update: TemporaryAccessGrantUpdate
    ) -> TemporaryAccessGrantDTO:
        resp = await self.request(
            "PATCH",
            f"temporary-access-grants/{grant_id}",
            json=update.model_dump(mode="json", exclude_unset=True),
        )
        return TemporaryAccessGrantDTO.model_validate(resp.json())

    # --- Stories ---

    async def get_story(self, story_id: str) -> StoryDTO:
        resp = await self.request("GET", f"stories/{story_id}")
        return StoryDTO.model_validate(resp.json())

    async def get_stories_by_status(self, status: str) -> list[StoryDTO]:
        resp = await self.request("GET", "stories/", params={"status": status})
        return [StoryDTO.model_validate(s) for s in resp.json()]

    async def get_stories_by_project(self, project_id: str) -> list[StoryDTO]:
        resp = await self.request("GET", "stories/", params={"project_id": project_id})
        return [StoryDTO.model_validate(s) for s in resp.json()]

    async def fail_story(self, story_id: str) -> StoryDTO:
        """Transition story to failed status."""
        resp = await self.request("POST", f"stories/{story_id}/fail", json={"actor": "supervisor"})
        return StoryDTO.model_validate(resp.json())

    async def wait_user_secret_story(self, story_id: str) -> StoryDTO:
        """Park a deploying story in WAITING_USER_SECRET until the secret appears."""
        resp = await self.request(
            "POST", f"stories/{story_id}/wait-user-secret", json={"actor": "supervisor"}
        )
        return StoryDTO.model_validate(resp.json())

    async def transition_story(self, story_id: str, action: str) -> StoryDTO:
        """Transition story status. action: 'start', 'complete', 'archive'."""
        resp = await self.request(
            "POST", f"stories/{story_id}/{action}", json={"actor": "architect"}
        )
        return StoryDTO.model_validate(resp.json())

    async def update_story(self, story_id: str, data: dict) -> StoryDTO:
        """Patch story fields (e.g. pr_number)."""
        resp = await self.request("PATCH", f"stories/{story_id}", json=data)
        return StoryDTO.model_validate(resp.json())

    # --- Applications ---

    async def stop_application(self, application_id: int) -> None:
        """Request a token-preserving lifecycle stop for an application."""
        await self.request(
            "POST",
            f"applications/{application_id}/stop",
            json={"actor": "supervisor"},
        )

    # --- Tasks ---

    async def get_tasks_by_status(self, status: str) -> list[TaskDTO]:
        resp = await self.request("GET", "tasks/", params={"status": status})
        return [TaskDTO.model_validate(t) for t in resp.json()]

    async def get_tasks_by_story(self, story_id: str) -> list[TaskDTO]:
        resp = await self.request("GET", "tasks/", params={"story_id": story_id})
        return [TaskDTO.model_validate(t) for t in resp.json()]

    async def get_tasks_by_project_and_status(
        self,
        project_id: str,
        status: str,
    ) -> list[TaskDTO]:
        resp = await self.request(
            "GET",
            "tasks/",
            params={"project_id": project_id, "status": status},
        )
        return [TaskDTO.model_validate(t) for t in resp.json()]

    async def create_task(self, task_data: dict) -> TaskDTO:
        resp = await self.request("POST", "tasks/", json=task_data)
        return TaskDTO.model_validate(resp.json())

    async def update_task(self, task_id: str, data: dict) -> TaskDTO:
        resp = await self.request("PATCH", f"tasks/{task_id}", json=data)
        return TaskDTO.model_validate(resp.json())

    async def get_task(self, task_id: str) -> TaskDTO:
        resp = await self.request("GET", f"tasks/{task_id}")
        return TaskDTO.model_validate(resp.json())

    async def transition_task(
        self,
        task_id: str,
        to_status: str,
        actor: str = "architect",
        *,
        details: dict[str, Any] | None = None,
    ) -> TaskDTO:
        resp = await self.request(
            "POST",
            f"tasks/{task_id}/transition",
            params={"to_status": to_status},
            json={"actor": actor, "details": details or {}},
        )
        return TaskDTO.model_validate(resp.json())

    async def create_task_event(self, task_id: str, event: dict) -> TaskEventDTO:
        resp = await self.request("POST", f"tasks/{task_id}/events", json=event)
        return TaskEventDTO.model_validate(resp.json())

    async def get_task_events(self, task_id: str) -> list[TaskEventDTO]:
        resp = await self.request("GET", f"tasks/{task_id}/events")
        return [TaskEventDTO.model_validate(e) for e in resp.json()]

    # --- Incidents ---

    async def create_incident(
        self,
        server_handle: str | None,
        incident_type: str,
        details: dict,
        affected_services: list[str] | None = None,
    ) -> IncidentDTO:
        resp = await self.request(
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
        resp = await self.request(
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

        resp = await self.request(
            "PATCH",
            f"incidents/{incident_id}",
            json={
                "status": "resolved",
                "resolved_at": datetime.now(UTC).isoformat(),
            },
        )
        return IncidentDTO.model_validate(resp.json())

    async def list_active_incidents(self) -> list[IncidentDTO]:
        """Return detected and recovering incidents for journal reconciliation."""
        resp = await self.request("GET", "incidents/active")
        return [IncidentDTO.model_validate(incident) for incident in resp.json()]

    # --- Metrics History ---

    async def create_metrics_history(self, server_handle: str, metrics: dict) -> dict:
        resp = await self.request(
            "POST",
            f"servers/{server_handle}/metrics-history",
            json={"metrics": metrics},
        )
        return resp.json()

    async def delete_old_metrics_history(self, retention_hours: int = 168) -> dict:
        resp = await self.request(
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
        resp = await self.request("GET", "applications/", params=params)
        return [ApplicationDTO.model_validate(a) for a in resp.json()]

    async def get_application_if_missing_returns_none(
        self, application_id: int
    ) -> ApplicationDTO | None:
        """One application by id, or None if the record is gone.

        A caller that has to read the machine a particular deployment runs on
        asks for it by id rather than picking one of the project's. Missing is
        an answer here: the deployment it names is not there to be read.
        """
        try:
            resp = await self.request("GET", f"applications/{application_id}")
        except httpx.HTTPStatusError as error:
            if error.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise
        return ApplicationDTO.model_validate(resp.json())

    async def update_application(self, app_id: int, fields: dict) -> ApplicationDTO:
        """Update application fields (status, health metrics, etc.)."""
        resp = await self.request("PATCH", f"applications/{app_id}", json=fields)
        return ApplicationDTO.model_validate(resp.json())

    async def create_app_health_history(self, app_id: int, metrics: dict) -> dict:
        """Append a health history snapshot for an application."""
        resp = await self.request(
            "POST",
            f"applications/{app_id}/health-history",
            json={"metrics": metrics},
        )
        return resp.json()

    async def delete_old_app_health_history(self, retention_hours: int = 168) -> dict:
        """Delete application health history older than retention period."""
        resp = await self.request(
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
            resp = await self.request("GET", "applications/", params={"repo_id": repo.id})
            results.extend(ApplicationDTO.model_validate(a) for a in resp.json())
        return results

    # --- Users ---

    async def get_user(self, user_id: int) -> UserDTO | None:
        try:
            resp = await self.request("GET", f"users/{user_id}")
            return UserDTO.model_validate(resp.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    # --- API Keys ---

    async def get_api_key(self, service: str) -> dict | None:
        try:
            resp = await self.request("GET", f"api-keys/{service}")
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise

    # --- Analytics ---

    async def upsert_analytics_hourly(self, data: dict) -> dict:
        """Upsert an hourly analytics row."""
        resp = await self.request("POST", "analytics/hourly", json=data)
        return resp.json()

    async def upsert_analytics_daily(self, data: dict) -> dict:
        """Upsert a daily analytics row."""
        resp = await self.request("POST", "analytics/daily", json=data)
        return resp.json()

    async def upsert_known_users(self, project_id: str, users: list[dict]) -> dict:
        """Batch upsert known users for a project."""
        resp = await self.request(
            "POST",
            "analytics/known-users",
            json={"project_id": project_id, "users": users},
        )
        return resp.json()

    async def get_known_users(self, project_id: str) -> list[dict]:
        """Get known users for a project."""
        resp = await self.request(
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
        resp = await self.request("GET", "analytics/hourly", params=params)
        return resp.json()

    async def delete_old_hourly(self, days: int) -> dict:
        """Delete hourly analytics older than N days."""
        resp = await self.request(
            "DELETE",
            "analytics/hourly",
            params={"older_than_days": days},
        )
        return resp.json()

    async def delete_old_daily(self, days: int) -> dict:
        """Delete daily analytics older than N days."""
        resp = await self.request(
            "DELETE",
            "analytics/daily",
            params={"older_than_days": days},
        )
        return resp.json()

    # --- System configs ---

    async def upsert_system_config(
        self,
        key: str,
        value: str,
        category: str,
        description: str,
    ) -> dict:
        """Create or overwrite a system config entry."""
        resp = await self.request(
            "POST",
            "system-configs/",
            json={
                "key": key,
                "value": value,
                "category": category,
                "description": description,
                "updated_by": "scheduler",
            },
        )
        return resp.json()


api_client = SchedulerAPIClient()
