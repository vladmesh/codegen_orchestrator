"""DTO factory helpers for unit tests.

Provides convenience functions that create Pydantic DTOs with sensible defaults,
so test mocks return the same types as the real API client.
"""

from datetime import UTC, datetime
import uuid

from shared.contracts.dto.deploy_dispatch import DeployRunStart
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.server import ServerDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO
from shared.contracts.dto.user import UserDTO
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL

_NOW = datetime.now(UTC)
_PROJECT_ID = uuid.uuid4()


def make_project(**overrides) -> ProjectDTO:
    base = {
        "id": _PROJECT_ID,
        "initiating_run_id": "test-run-1",
        "title": "test-project",
        "slug": "test-project-0000",
        "status": ProjectStatus.ACTIVE,
        "config": {},
        "owner_id": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return ProjectDTO(**base)


def make_run(**overrides) -> RunDTO:
    """The run record a consumer reads before acting on its message."""
    base = {
        "id": "deploy-1",
        "project_id": str(_PROJECT_ID),
        "type": RunType.DEPLOY,
        "status": RunStatus.QUEUED,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return RunDTO(**base)


def make_run_start(**overrides) -> DeployRunStart:
    """The answer a consumer gets when it takes its run to RUNNING."""
    base = {"run_id": "deploy-1", "started": True, "run_status": RunStatus.RUNNING}
    base.update(overrides)
    return DeployRunStart(**base)


def make_repository(**overrides) -> RepositoryDTO:
    base = {
        "id": "repo-1",
        "project_id": _PROJECT_ID,
        "name": "test-project",
        "git_url": "https://github.com/org/test-project",
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return RepositoryDTO(**base)


def make_story(**overrides) -> StoryDTO:
    base = {
        "id": "story-abc",
        "project_id": _PROJECT_ID,
        "title": "Test story",
        "type": "product",
        "status": "created",
        "priority": 0,
        "created_by": "system",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return StoryDTO(**base)


def make_task(**overrides) -> TaskDTO:
    base = {
        "id": "task-1",
        "project_id": _PROJECT_ID,
        "type": "feature",
        "title": "Test task",
        "status": "todo",
        "priority": 0,
        "current_iteration": 1,
        "max_iterations": 3,
        "created_by": "system",
        "dispatch_admitted": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return TaskDTO(**base)


def make_task_event(**overrides) -> TaskEventDTO:
    base = {
        "id": 1,
        "task_id": "task-1",
        "event_type": "status_change",
        "actor": "system",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return TaskEventDTO(**base)


def make_server(**overrides) -> ServerDTO:
    """A finished managed host. Override `labels` to model an unprovisioned one."""
    base = {
        "handle": "srv-1",
        "host": "srv-1.example.com",
        "public_ip": "1.2.3.4",
        "ssh_user": "dev",
        "status": "ready",
        "is_managed": True,
        "capacity_ram_mb": 4096,
        "capacity_disk_mb": 50000,
        # Admission refuses a host whose software provisioning is not recorded
        # complete, so the default server here is one that finished provisioning.
        "labels": {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return ServerDTO(**base)


def make_user(**overrides) -> UserDTO:
    base = {
        "id": 1,
        "telegram_id": 12345,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return UserDTO(**base)
