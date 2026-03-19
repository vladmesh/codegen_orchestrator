from enum import StrEnum
from typing import Literal

from shared.contracts.base import BaseMessage, BaseResult


class DeployTrigger(StrEnum):
    """Origin of a deploy request."""

    ENGINEERING = "engineering"
    WEBHOOK = "webhook"
    PO = "po"


class DeployOutcome(StrEnum):
    """Outcome stored in run.result for dispatcher consumption."""

    SUCCESS = "success"
    SMOKE_FAILURE = "smoke_failure"
    CODE_FIX = "code_fix"
    RETRY = "retry"
    GIVE_UP = "give_up"


class DeployMessage(BaseMessage):
    """Start deploy task."""

    task_id: str
    project_id: str
    user_id: str = ""
    story_id: str = ""
    triggered_by: DeployTrigger = DeployTrigger.ENGINEERING
    action: Literal["create", "feature", "fix"] = "create"
    deploy_fix_attempt: int = 0


class DeployResult(BaseResult):
    """Deploy task result."""

    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None
