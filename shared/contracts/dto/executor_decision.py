"""Immutable executor choices made when a paid Run is admitted."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.run import RunType
from shared.contracts.vocab import QA_EXECUTOR_AGENT_TYPES, AgentType

#: The only ``Run.run_metadata`` key that contains the paid-run executor snapshot.
EXECUTOR_DECISION_METADATA_KEY = "executor_decision"


class ExecutorDecisionSource(StrEnum):
    """The policy input that selected a paid attempt's executor."""

    PROJECT_PIN = "project_pin"
    API_DEFAULT = "api_default"
    QA_API_SETTING = "qa_api_setting"
    GLOBAL_OVERRIDE = "global_override"


class ExecutorOverride(StrEnum):
    """The only operator-selected break-glass executors for paid attempts."""

    NONE = "none"
    CLAUDE = "claude"
    CODEX = "codex"


class ExecutorDecision(BaseModel):
    """A complete, typed, immutable executor choice for one paid attempt."""

    model_config = ConfigDict(extra="forbid")

    attempt_kind: Literal[RunType.ENGINEERING, RunType.QA]
    agent_type: AgentType
    source: ExecutorDecisionSource
    policy_version: Literal["v1", "v2"]
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _validate_attempt_executor_pair(self) -> "ExecutorDecision":
        if self.attempt_kind is RunType.QA and self.agent_type not in QA_EXECUTOR_AGENT_TYPES:
            allowed = ", ".join(sorted(agent.value for agent in QA_EXECUTOR_AGENT_TYPES))
            raise ValueError(f"QA executor must be one of {allowed}, not {self.agent_type.value}")
        return self

    def as_run_metadata(self) -> dict[str, Any]:
        """Return the single metadata fragment persisted during paid-run creation."""
        return {EXECUTOR_DECISION_METADATA_KEY: self.model_dump(mode="json")}

    @classmethod
    def from_run_metadata(cls, metadata: dict[str, Any] | None) -> "ExecutorDecision":
        """Read the required snapshot without accepting a partial or untyped record."""
        if not isinstance(metadata, dict):
            raise ValueError("Run metadata is missing the executor decision")
        decision = metadata.get(EXECUTOR_DECISION_METADATA_KEY)
        if decision is None:
            raise ValueError("Run metadata is missing the executor decision")
        return cls.model_validate(decision)
