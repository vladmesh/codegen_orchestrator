from enum import StrEnum

from shared.contracts.base import BaseMessage, BaseResult


class DeployTrigger(StrEnum):
    """Origin of a deploy request."""

    ENGINEERING = "engineering"
    WEBHOOK = "webhook"
    PO = "po"
    ADMIN = "admin"


class DeployAction(StrEnum):
    """Type of deploy operation."""

    CREATE = "create"
    FEATURE = "feature"
    FIX = "fix"
    STOP = "stop"
    UNDEPLOY = "undeploy"


class DeployOutcome(StrEnum):
    """Outcome stored in run.result for dispatcher consumption."""

    SUCCESS = "success"
    SMOKE_FAILURE = "smoke_failure"
    CODE_FIX = "code_fix"
    RETRY = "retry"
    GIVE_UP = "give_up"
    WAITING_FOR_USER_SECRET = "waiting_for_user_secret"  # noqa: S105
    ALLOCATION_MISSING = "allocation_missing"
    ENVIRONMENT_CONTRACT_INVALID = "environment_contract_invalid"
    ENVIRONMENT_RESOLUTION_FAILED = "environment_resolution_failed"
    HEAD_SHA_MISSING = "head_sha_missing"


class DeployMessage(BaseMessage):
    """Start deploy task."""

    task_id: str
    project_id: str
    user_id: str = ""
    story_id: str = ""
    triggered_by: DeployTrigger = DeployTrigger.ENGINEERING
    action: DeployAction = DeployAction.CREATE
    deploy_fix_attempt: int = 0
    # Required for commit-deploy actions. Lifecycle actions keep this empty
    # because they do not read or deploy repository state.
    head_sha: str = ""


class DeployResult(BaseResult):
    """Deploy task result."""

    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None
