from dataclasses import dataclass
from enum import StrEnum

from shared.contracts.base import BaseMessage


class QAOutcome(StrEnum):
    """Outcome stored in run.result for dispatcher consumption."""

    PASSED = "passed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    ERROR = "error"


class QAMessage(BaseMessage):
    """Trigger QA testing for a deployed project."""

    story_id: str
    project_id: str
    user_id: str
    deployed_url: str
    application_id: int
    run_id: str = ""
    bot_username: str | None = None
    qa_attempt: int = 0


@dataclass(frozen=True)
class QAServerInfo:
    """Resolved server connection info for QA testing."""

    server_ip: str
    ssh_key: str
    project_name: str
