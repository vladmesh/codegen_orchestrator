from dataclasses import dataclass

from shared.contracts.base import BaseMessage


class QAMessage(BaseMessage):
    """Trigger QA testing for a deployed project."""

    story_id: str
    project_id: str
    user_id: str
    deployed_url: str
    application_id: int
    bot_username: str | None = None
    qa_attempt: int = 0


@dataclass(frozen=True)
class QAServerInfo:
    """Resolved server connection info for QA testing."""

    server_ip: str
    ssh_key: str
    project_name: str
