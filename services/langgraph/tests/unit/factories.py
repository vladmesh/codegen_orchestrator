"""DTO factory helpers for unit tests.

Provides convenience functions that create Pydantic DTOs with sensible defaults,
so test mocks return the same types as the real API client.
"""

from datetime import UTC, datetime
import uuid

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.server import ServerDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO
from shared.contracts.dto.user import UserDTO

_NOW = datetime.now(UTC)
_PROJECT_ID = uuid.uuid4()


def make_project(**overrides) -> ProjectDTO:
    base = {
        "id": _PROJECT_ID,
        "name": "test-project",
        "status": ProjectStatus.ACTIVE,
        "config": {},
        "owner_id": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return ProjectDTO(**base)


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
    base = {
        "handle": "srv-1",
        "host": "srv-1.example.com",
        "public_ip": "1.2.3.4",
        "ssh_user": "dev",
        "status": "ready",
        "is_managed": True,
        "capacity_ram_mb": 4096,
        "capacity_disk_mb": 50000,
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
