from enum import StrEnum

from pydantic import BaseModel, model_validator

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


class RunCreate(BaseModel):
    """Create run request."""

    project_id: str
    type: RunType
    spec: str | None = None


class RunDTO(TimestampedDTO):
    """Run response.

    `result` is typed per `type`: a deploy run carries a `DeployRunResult`, a QA
    run a `QARunResult`, an engineering run an `EngineeringRunResult`. `None`
    means no result yet (queued/running) or a failure that produced no structured
    result. A payload of the wrong type is rejected, so consumers read outcomes
    through typed attributes instead of guessing dict keys.
    """

    id: str
    project_id: str
    type: RunType
    status: RunStatus
    story_id: str | None = None
    spec: str | None = None
    result: RunResult | None = None

    @model_validator(mode="after")
    def _check_result_matches_type(self) -> "RunDTO":
        if self.result is None:
            return self
        expected = _RESULT_MODEL_BY_TYPE[self.type]
        if not isinstance(self.result, expected):
            raise ValueError(
                f"run.result is {type(self.result).__name__} but run.type is {self.type.value}"
            )
        return self
