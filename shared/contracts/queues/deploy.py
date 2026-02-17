from enum import Enum

from shared.contracts.base import BaseMessage, BaseResult


class DeployTrigger(str, Enum):
    """Origin of a deploy request."""

    ENGINEERING = "engineering"
    WEBHOOK = "webhook"
    PO = "po"


class DeployMessage(BaseMessage):
    """Start deploy task."""

    task_id: str
    project_id: str
    user_id: str = ""
    triggered_by: DeployTrigger = DeployTrigger.ENGINEERING


class DeployResult(BaseResult):
    """Deploy task result."""

    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None
