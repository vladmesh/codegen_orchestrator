from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.run_result import (
    DeployRunResult,
    EngineeringRunResult,
    QARunResult,
    RunResult,
)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunType(StrEnum):
    ENGINEERING = "engineering"
    DEPLOY = "deploy"
    QA = "qa"


_RESULT_MODEL_BY_TYPE: dict[RunType, type] = {
    RunType.ENGINEERING: EngineeringRunResult,
    RunType.DEPLOY: DeployRunResult,
    RunType.QA: QARunResult,
}

# A run that reached COMPLETED or FAILED has produced its outcome, so it must
# carry a typed result. CANCELLED (e.g. a deploy superseded by a lock holder)
# and the in-flight states (QUEUED/RUNNING) legitimately have no result yet.
_TERMINAL_STATUSES_REQUIRING_RESULT: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED}
)


class RunCreate(BaseModel):
    """Create run request."""

    project_id: str
    type: RunType
    spec: str | None = None


class RunDTO(TimestampedDTO):
    """Run response.

    `result` is typed per `type`: a deploy run carries a `DeployRunResult`, a QA
    run a `QARunResult`, an engineering run an `EngineeringRunResult`. A payload
    of the wrong type is rejected, so consumers read outcomes through typed
    attributes instead of guessing dict keys.

    `result=None` is allowed only while the outcome has not appeared —
    QUEUED/RUNNING, or a CANCELLED (superseded) run. A COMPLETED or FAILED run
    without a result is rejected, so a terminal run that lost its outcome
    surfaces loudly instead of being silently skipped forever.
    """

    id: str
    project_id: str
    type: RunType
    status: RunStatus
    story_id: str | None = None
    spec: str | None = None
    run_metadata: dict[str, Any] = Field(default_factory=dict)
    result: RunResult | None = None

    @model_validator(mode="after")
    def _check_result(self) -> "RunDTO":
        if self.result is None:
            if self.status in _TERMINAL_STATUSES_REQUIRING_RESULT:
                raise ValueError(f"run.result is required when status is {self.status.value}")
            return self
        expected = _RESULT_MODEL_BY_TYPE[self.type]
        if not isinstance(self.result, expected):
            raise ValueError(
                f"run.result is {type(self.result).__name__} but run.type is {self.type.value}"
            )
        return self
