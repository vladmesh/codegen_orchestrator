"""Strict, bounded read contract for the production administrator overview."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.executor_decision import ExecutorDecision
from shared.contracts.dto.run import RunType
from shared.contracts.vocab import AgentType


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueueStreamInfo(_StrictModel):
    length: int = Field(ge=0)


class QueueGroupInfo(_StrictModel):
    consumers: int = Field(ge=0)
    pending: int = Field(ge=0)
    last_delivered_id: str


class QueueBindingSnapshot(_StrictModel):
    stream: str
    group: str
    description: str
    stream_info: QueueStreamInfo | None
    group_info: QueueGroupInfo | None


class QueueHealthSnapshot(_StrictModel):
    status: Literal["ok", "degraded"]
    bindings: list[QueueBindingSnapshot]
    issues: list[str]


class TaskStatusCounts(_StrictModel):
    backlog: int = Field(default=0, ge=0)
    todo: int = Field(default=0, ge=0)
    in_dev: int = Field(default=0, ge=0)
    in_ci: int = Field(default=0, ge=0)
    testing: int = Field(default=0, ge=0)
    done: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    waiting_human_review: int = Field(default=0, ge=0)
    waiting_resources: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)


class PaidRunStateCounts(_StrictModel):
    queued: int = Field(ge=0)
    running: int = Field(ge=0)


class PaidRunCounts(_StrictModel):
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    by_executor: dict[AgentType, PaidRunStateCounts]
    unavailable_executor_decisions: int = Field(ge=0)


class ExecutorDecisionAvailability(StrEnum):
    AVAILABLE = "available"
    LEGACY = "legacy"
    INVALID = "invalid"


class RecentFailedRun(_StrictModel):
    id: str
    type: RunType
    project_id: uuid.UUID | None
    task_id: str | None
    story_id: str | None
    error_message: str = Field(max_length=2000)
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    executor_decision: ExecutorDecision | None
    executor_decision_availability: ExecutorDecisionAvailability


class AdminOverviewResponse(_StrictModel):
    queues: QueueHealthSnapshot
    task_counts: TaskStatusCounts
    paid_runs: PaidRunCounts
    recent_failed_runs: list[RecentFailedRun]


__all__ = [
    "AdminOverviewResponse",
    "ExecutorDecisionAvailability",
    "PaidRunCounts",
    "PaidRunStateCounts",
    "QueueBindingSnapshot",
    "QueueGroupInfo",
    "QueueHealthSnapshot",
    "QueueStreamInfo",
    "RecentFailedRun",
    "TaskStatusCounts",
]
