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
    BLOCKED = "blocked"


class QAMessage(BaseMessage):
    """Trigger QA testing for a deployed project."""

    story_id: str = ""
    project_id: str
    # The run that asked for this work, exactly as on `EngineeringMessage`: the
    # project's `initiating_run_id`, carried by the producer. A QA executor is
    # owned by the same run as the developer workers of the same project, which
    # is what lets one run account for every container it caused.
    initiating_run_id: str = Field(min_length=1)
    # Telegram chat of the project owner, resolved by the producer. Empty when
    # the work was started by the system and has no user to report back to.
    telegram_chat_id: str = ""
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
    """Resolved server connection info for QA testing.

    `allocated_ports` is deployment data, not a runtime observation: it is what
    the platform gave this application, and it is what bounds the loopback probe
    a central QA run may make. A port nobody allocated to this deployment is not
    this deployment's, whatever happens to be listening on it.
    """

    server_ip: str
    # The administrative account the stored key opens. QA uses it to install and
    # remove the run's own key, and for nothing else.
    ssh_user: SSHUser
    # The unprivileged account provisioning created for QA runs on this host,
    # read from the server row. Empty when this host lends none — which is a
    # refusal, not a reason to fall back to `ssh_user`.
    qa_ssh_user: str
    ssh_key: str
    project_name: str
    server_handle: str = ""
    allocated_ports: frozenset[int] = frozenset()
    # Why this host lends no QA identity, when it lends none. Exactly one of this
    # and `qa_ssh_user` is set: the reason travels with the resolution so the
    # refusal can be journalled where it is decided rather than re-derived.
    qa_identity_rejection: str = ""
