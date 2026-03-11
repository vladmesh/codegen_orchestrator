from enum import StrEnum
from typing import Literal

from shared.contracts.base import BaseMessage, BaseResult


class DeployTrigger(StrEnum):
    """Origin of a deploy request."""

    ENGINEERING = "engineering"
    WEBHOOK = "webhook"
    PO = "po"


class DeployMessage(BaseMessage):
    """Start deploy task."""

    task_id: str
    project_id: str
    user_id: str = ""
    story_id: str = ""
    triggered_by: DeployTrigger = DeployTrigger.ENGINEERING
    action: Literal["create", "feature", "fix"] = "create"


class DeployResult(BaseResult):
    """Deploy task result."""

    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None
