from pydantic import Field

from shared.contracts.base import BaseMessage, BaseResult
from shared.contracts.vocab import ActionType


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

    # `task_id` is this attempt's engineering Run row id: the producer creates
    # the run and names the message after it. It is an attempt, not a run
    # identity — see `initiating_run_id`.
    task_id: str
    project_id: str
    # The run that asked for this work: a live harness run, a matrix
    # combination. It is written down once, when the project is created, and
    # every producer carries it from there — a message cannot be built without
    # it, so no engineering worker can be created unowned. One initiating run
    # may spawn many engineering attempts; this identity is what run-scoped
    # cleanup and per-run evidence are decidable against.
    initiating_run_id: str = Field(min_length=1)
    # Telegram chat of the project owner, resolved by the producer. Empty when
    # the work was started by the system and has no user to report back to.
    telegram_chat_id: str = ""
    action: ActionType = ActionType.CREATE
    description: str | None = None
    skip_deploy: bool = False
    planning_task_id: str | None = None  # planning-layer Task ID for status updates
    story_id: str | None = None  # story ID for worker reuse across tasks
    deploy_fix_attempt: int = 0  # tracks deploy→engineering retry count
    branch: str | None = None  # story branch name (e.g. "story/{story_id}")


class EngineeringResult(BaseResult):
    """Engineering task result."""

    files_changed: list[str] | None = None
    commit_sha: str | None = None
    branch: str | None = None
