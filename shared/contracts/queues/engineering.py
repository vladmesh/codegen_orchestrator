from shared.contracts.base import BaseMessage, BaseResult


class EngineeringMessage(BaseMessage):
    """Start engineering task."""

    task_id: str
    project_id: str
    user_id: int


class EngineeringResult(BaseResult):
    """Engineering task result."""

    files_changed: list[str] | None = None
    commit_sha: str | None = None
    branch: str | None = None
