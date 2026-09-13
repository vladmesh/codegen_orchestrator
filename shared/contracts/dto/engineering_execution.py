"""Typed evidence for the engineering boundary before an agent starts."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENGINEERING_INFRASTRUCTURE_KEY = "engineering_infrastructure"


class EngineeringExecutionPhase(StrEnum):
    """The two execution facts relevant to free infrastructure recovery."""

    AGENT_STARTED = "agent_started"
    PRE_AGENT_REFUSED = "pre_agent_refused"


class EngineeringInfrastructureRefusal(StrEnum):
    """Infrastructure reasons that can stop engineering before agent startup."""

    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    EXECUTOR_CONFIRMATION_REQUIRED = "executor_confirmation_required"
    PROJECT_LOCKED = "project_locked"
    WORKER_PROFILE_UNAVAILABLE = "worker_profile_unavailable"
    WORKER_CREATION_FAILED = "worker_creation_failed"


class EngineeringExecutionEvidence(BaseModel):
    """Validated evidence of whether an engineering attempt reached an agent."""

    model_config = ConfigDict(extra="forbid")

    execution_phase: EngineeringExecutionPhase
    infrastructure_refusal: EngineeringInfrastructureRefusal | None = None

    @model_validator(mode="after")
    def _refusal_matches_phase(self) -> "EngineeringExecutionEvidence":
        if self.execution_phase is EngineeringExecutionPhase.PRE_AGENT_REFUSED:
            if self.infrastructure_refusal is None:
                raise ValueError("pre_agent_refused requires infrastructure_refusal")
        elif self.infrastructure_refusal is not None:
            raise ValueError("agent_started prohibits infrastructure_refusal")
        return self


class EngineeringInfrastructurePark(BaseModel):
    """Exact evidence that authorises the one infrastructure retry action."""

    model_config = ConfigDict(extra="forbid")

    execution_phase: EngineeringExecutionPhase = EngineeringExecutionPhase.PRE_AGENT_REFUSED
    refusal: EngineeringInfrastructureRefusal
    task_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    detail: str = Field(min_length=1)

    @model_validator(mode="after")
    def _is_pre_agent_only(self) -> "EngineeringInfrastructurePark":
        if self.execution_phase is not EngineeringExecutionPhase.PRE_AGENT_REFUSED:
            raise ValueError("an infrastructure park must be pre_agent_refused")
        return self

    def as_metadata(self) -> dict[str, dict[str, Any]]:
        return {ENGINEERING_INFRASTRUCTURE_KEY: self.model_dump(mode="json")}


class EngineeringInfrastructureParkCommand(BaseModel):
    """One exact story-backed park the API applies to task and story atomically."""

    model_config = ConfigDict(extra="forbid")

    park: EngineeringInfrastructurePark
    actor: str = Field(min_length=1)


class EngineeringInfrastructureParkDisposition(StrEnum):
    """Committed result of one infrastructure park command."""

    #: This call moved task and story to human review and owed the owner notice.
    PARKED = "parked"
    #: Task and story already carry this exact park; nothing was written.
    ALREADY_PARKED = "already_parked"
    #: The story cannot legally reach human review; neither row was changed.
    INELIGIBLE_STORY = "ineligible_story"


class EngineeringInfrastructureParkRead(BaseModel):
    """Typed result of the atomic infrastructure park transaction."""

    model_config = ConfigDict(extra="forbid")

    disposition: EngineeringInfrastructureParkDisposition
    story_id: str
    task_id: str
    attempt_id: str
    refusal: EngineeringInfrastructureRefusal
    task_status: str
    story_status: str
    current_iteration: int


class EngineeringInfrastructureRetryCommand(BaseModel):
    """Exact park identity an operator asks the composite action to recover."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    refusal: EngineeringInfrastructureRefusal
    actor: str = Field(default="admin", min_length=1)


class EngineeringInfrastructureRetryOutcome(StrEnum):
    RETRIED = "retried"
    ALREADY_RETRIED = "already_retried"


class EngineeringInfrastructureRetryRead(BaseModel):
    """Audited result of the composite infrastructure recovery action."""

    model_config = ConfigDict(extra="forbid")

    outcome: EngineeringInfrastructureRetryOutcome
    story_id: str
    task_id: str
    attempt_id: str
    refusal: EngineeringInfrastructureRefusal
    current_iteration: int


def infrastructure_refusal_for_dispatch(
    reason: object,
) -> EngineeringInfrastructureRefusal | None:
    """Map only recoverable admission reasons, without parsing prose."""
    value = getattr(reason, "value", reason)
    try:
        return EngineeringInfrastructureRefusal(value)
    except ValueError:
        return None


def infrastructure_refusal_detail(reason: EngineeringInfrastructureRefusal) -> str:
    """Stable operator-facing detail derived only from the typed reason."""
    return f"Engineering worker creation was refused: {reason.value.replace('_', ' ')}."
