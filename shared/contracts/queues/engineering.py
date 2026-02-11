from typing import Literal

from shared.contracts.base import BaseMessage, BaseResult


class EngineeringMessage(BaseMessage):
    """Start engineering task.

    Fields:
        action: Type of engineering work:
            - "create": New project (scaffold + develop + deploy)
            - "feature": Add feature to existing project (develop + deploy)
            - "fix": Fix issue in existing project (develop + deploy)
        description: Human-readable task description for the developer worker.
            Required for "feature" and "fix" actions.
    """

    task_id: str
    project_id: str
    user_id: int
    action: Literal["create", "feature", "fix"] = "create"
    description: str | None = None
    skip_deploy: bool = False


class EngineeringResult(BaseResult):
    """Engineering task result."""

    files_changed: list[str] | None = None
    commit_sha: str | None = None
    branch: str | None = None
