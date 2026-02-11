from enum import Enum

from shared.contracts.base import BaseMessage, BaseResult
from shared.contracts.dto.project import ServiceModule


class ScaffolderAction(str, Enum):
    """Action type for scaffolder messages."""

    CREATE = "create"
    UPDATE = "update"


class ScaffolderMessage(BaseMessage):
    """
    Scaffold or update project structure.

    Responsibilities for CREATE:
    1. Create remote repository (if not exists).
    2. Generate .project.yml config.
    3. Run copier template.
    4. Push initial commit.

    Responsibilities for UPDATE:
    1. Clone existing repository.
    2. Run copier update --defaults.
    3. Commit and push changes.
    """

    action: ScaffolderAction = ScaffolderAction.CREATE
    project_id: str
    project_name: str
    repo_full_name: str  # org/repo format, e.g. "vladmesh/my-project"
    modules: list[ServiceModule] = []  # Not required for update action
    task_description: str = ""  # Detailed task description for TASK.md


class ScaffolderResult(BaseResult):
    """
    Scaffolder result.
    Stream: scaffolder:results
    """

    project_id: str
    repo_url: str
    files_generated: int = 0
