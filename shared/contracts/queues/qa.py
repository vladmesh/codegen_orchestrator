from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field

from shared.contracts.base import BaseMessage
from shared.contracts.dto.server import SSHUser


class QAOutcome(StrEnum):
    """Outcome stored in run.result for dispatcher consumption."""

    PASSED = "passed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    ERROR = "error"


class QAMessage(BaseMessage):
    """Trigger QA testing for a deployed project."""

    story_id: str = ""
    project_id: str
    user_id: str
    deployed_url: str
    application_id: int
    # What QA tests the deployment against, resolved from the repository by the
    # producer. Blank would parse to "no checks" and quietly reach the agent with
    # nothing to test, so the contract rejects it rather than QA discovering it.
    acceptance_criteria: str = Field(min_length=1)
    run_id: str = ""
    bot_username: str | None = None
    qa_attempt: int = 0


@dataclass(frozen=True)
class QAServerInfo:
    """Resolved server connection info for QA testing."""

    server_ip: str
    ssh_user: SSHUser
    ssh_key: str
    project_name: str
