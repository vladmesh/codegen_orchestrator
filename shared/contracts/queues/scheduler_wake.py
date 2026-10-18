"""Nonpersistent scheduler wake hints carried over native Redis Pub/Sub.

A hint only names the loop that should reread its durable state soon. Delivery is lossy and
coalescible, and a hint grants no permission to transition a Story, Task or Run.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from shared.diagnostics import safe_validation_errors

SCHEDULER_WAKE_CHANNEL = "scheduler:wake"
# Bounds the work and the diagnostics one hostile message can cost the listener.
MAX_WAKE_HINT_CHARS = 256


class SchedulerWakeTarget(StrEnum):
    """Scheduler loops that accept a wake hint."""

    STORY_COMPLETION = "story_completion"
    TASK_DISPATCHER = "task_dispatcher"
    LIFECYCLE_SUPERVISION = "lifecycle_supervision"
    TEMPORARY_ACCESS = "temporary_access"
    QA_ROUTING = "qa_routing"


class SchedulerWakeHint(BaseModel):
    """A routing-only request that one scheduler loop runs its next cycle early."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: SchedulerWakeTarget


class SchedulerWakeHintRejected(ValueError):
    """A Pub/Sub payload that is not a supported wake hint; `diagnostics` never echoes it."""

    def __init__(self, reason: str, **diagnostics: object) -> None:
        super().__init__(reason)
        self.diagnostics: dict[str, object] = {"reason": reason, **diagnostics}


def parse_scheduler_wake_hint(data: object) -> SchedulerWakeHint:
    """Validate one Pub/Sub payload as a wake hint or raise `SchedulerWakeHintRejected`."""
    if not isinstance(data, str):
        raise SchedulerWakeHintRejected("not_text", payload_type=type(data).__name__)
    if len(data) > MAX_WAKE_HINT_CHARS:
        raise SchedulerWakeHintRejected("oversized", chars=len(data))
    try:
        return SchedulerWakeHint.model_validate_json(data)
    except ValidationError as exc:
        raise SchedulerWakeHintRejected("invalid", errors=safe_validation_errors(exc)) from None
