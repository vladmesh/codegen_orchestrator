"""WorkItem DTOs and enums — single source of truth for work item statuses and types."""

from enum import StrEnum


class WorkItemStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_DEV = "in_dev"
    IN_REVIEW = "in_review"
    TESTING = "testing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkItemType(StrEnum):
    CREATE = "create"
    FEATURE = "feature"
    FIX = "fix"
    REFACTOR = "refactor"


class WorkItemEventType(StrEnum):
    STATUS_CHANGE = "status_change"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    NOTE = "note"
    COMMENT = "comment"


# Valid status transitions: from_status -> set of allowed to_statuses
VALID_TRANSITIONS: dict[WorkItemStatus, set[WorkItemStatus]] = {
    WorkItemStatus.BACKLOG: {WorkItemStatus.TODO, WorkItemStatus.CANCELLED},
    WorkItemStatus.TODO: {WorkItemStatus.IN_DEV, WorkItemStatus.BACKLOG, WorkItemStatus.CANCELLED},
    WorkItemStatus.IN_DEV: {
        WorkItemStatus.IN_REVIEW,
        WorkItemStatus.TESTING,
        WorkItemStatus.FAILED,
        WorkItemStatus.CANCELLED,
    },
    WorkItemStatus.IN_REVIEW: {
        WorkItemStatus.IN_DEV,
        WorkItemStatus.TESTING,
        WorkItemStatus.FAILED,
        WorkItemStatus.CANCELLED,
    },
    WorkItemStatus.TESTING: {
        WorkItemStatus.DONE,
        WorkItemStatus.IN_DEV,
        WorkItemStatus.FAILED,
        WorkItemStatus.CANCELLED,
    },
    WorkItemStatus.DONE: {WorkItemStatus.BACKLOG},
    WorkItemStatus.FAILED: {WorkItemStatus.BACKLOG, WorkItemStatus.CANCELLED},
    WorkItemStatus.CANCELLED: set(),
}
