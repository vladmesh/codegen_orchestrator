from enum import StrEnum

from pydantic import Field, model_validator

from shared.contracts.base import BaseMessage, BaseResult
from shared.contracts.git_ref import OptionalCommitSha


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


# Actions that act on one already-deployed application instead of on repository state.
LIFECYCLE_ACTIONS = frozenset({DeployAction.STOP, DeployAction.UNDEPLOY})


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
    OWNER_ACCESS_PROOF_FAILED = "owner_access_proof_failed"
    HEAD_SHA_MISSING = "head_sha_missing"
    # The deploy never started: no server could be allocated for the application,
    # for a reason that is about the platform and not about the project (see
    # `shared/allocation_disposition.py`). The story is not failed — it waits and
    # is re-dispatched once a target is admissible again.
    WAITING_INFRASTRUCTURE = "waiting_infrastructure"
    # Somebody stopped this deploy on purpose: a fence taken by a deploy that has
    # to be the last writer, a teardown, or a withdrawal before it reached
    # GitHub. Nothing failed and nothing was deployed, so the story it belongs to
    # is redeployed rather than retried as a failure or left waiting.
    CANCELLED = "cancelled"


class DeployMessage(BaseMessage):
    """Start deploy task."""

    task_id: str
    project_id: str
    # Telegram chat of the project owner, resolved by the producer before it
    # publishes. A deploy with no user to report back to says so in
    # ``unaddressed_reason``; exactly one of the two is set, so a producer that
    # forgot to resolve a recipient cannot pass for one that deliberately has
    # none.
    telegram_chat_id: str = ""
    unaddressed_reason: str = ""
    story_id: str = ""
    triggered_by: DeployTrigger = DeployTrigger.ENGINEERING
    action: DeployAction = DeployAction.CREATE
    deploy_fix_attempt: int = 0
    # Required for commit-deploy actions. Lifecycle actions keep this empty
    # because they do not read or deploy repository state. A branch name is not
    # accepted here, so a caller that failed to resolve a SHA cannot silently
    # fall back to deploying the default branch.
    head_sha: OptionalCommitSha = ""
    # Which application a lifecycle action brings down. A project can run on
    # several servers, so the consumer must not pick one itself: it would stop a
    # container nobody asked about and leave the named one up.
    application_id: int | None = None
    # Non-secret environment values this deploy sets on top of the contract, for
    # state the caller turns on and off between deploys of the same commit — a
    # temporary test identity, for instance. Only keys the contract already
    # declares as literals may appear here; anything else is a contract change and
    # is rejected by the resolver rather than silently added to the environment.
    # Deploys of the same commit with different overrides are different deploys,
    # so the redundant-deploy shortcut compares them.
    env_overrides: dict[str, str] = Field(default_factory=dict)
    # Set when this deploy exists to take an effect away and must therefore be
    # the last one to write. The consumer then refuses the redundant-deploy
    # shortcut and the deployer proves every earlier Actions run of this
    # repository stopped before writing anything — the project deploy lock
    # expires and does not reach the run it started, so an abandoned deploy can
    # otherwise land after the one that cleared its value.
    fence_active_deploys: bool = False

    @model_validator(mode="after")
    def _names_a_recipient_or_says_why_not(self) -> "DeployMessage":
        if bool(self.telegram_chat_id) == bool(self.unaddressed_reason):
            raise ValueError(
                "set telegram_chat_id (the resolved Telegram chat of the owner) or "
                "unaddressed_reason (why this deploy reports to nobody), never both "
                "and never neither"
            )
        return self

    @model_validator(mode="after")
    def _lifecycle_names_its_target(self) -> "DeployMessage":
        if self.action in LIFECYCLE_ACTIONS and self.application_id is None:
            raise ValueError(f"application_id is required for action '{self.action.value}'")
        return self


class DeployResult(BaseResult):
    """Deploy task result."""

    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None
