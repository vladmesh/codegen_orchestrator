"""Milestone DTOs and enums — single source of truth for milestone statuses."""

from enum import StrEnum


class MilestoneStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"


# Valid status transitions: from_status -> set of allowed to_statuses
VALID_TRANSITIONS: dict[MilestoneStatus, set[MilestoneStatus]] = {
    MilestoneStatus.OPEN: {MilestoneStatus.COMPLETED},
    MilestoneStatus.COMPLETED: set(),
}
