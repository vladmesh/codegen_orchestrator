"""Task DTOs and enums — single source of truth for task statuses and types (planning layer)."""

from enum import StrEnum


class TaskStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_DEV = "in_dev"
    IN_CI = "in_ci"
    TESTING = "testing"
    DONE = "done"
    BLOCKED = "blocked"
    WAITING_HUMAN_REVIEW = "waiting_human_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(StrEnum):
    CREATE = "create"
    FEATURE = "feature"
    FIX = "fix"
    REFACTOR = "refactor"


class TaskEventType(StrEnum):
    STATUS_CHANGE = "status_change"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    NOTE = "note"
    COMMENT = "comment"
    WORKER_REPORT = "worker_report"


# Valid status transitions: from_status -> set of allowed to_statuses
VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.BACKLOG: {TaskStatus.TODO, TaskStatus.CANCELLED},
    TaskStatus.TODO: {TaskStatus.IN_DEV, TaskStatus.BACKLOG, TaskStatus.CANCELLED},
    TaskStatus.IN_DEV: {
        TaskStatus.IN_CI,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_HUMAN_REVIEW,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.IN_CI: {
        TaskStatus.IN_DEV,
        TaskStatus.TESTING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.TESTING: {
        TaskStatus.DONE,
        TaskStatus.IN_DEV,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.BLOCKED: {TaskStatus.IN_DEV, TaskStatus.BACKLOG, TaskStatus.CANCELLED},
    TaskStatus.WAITING_HUMAN_REVIEW: {
        TaskStatus.IN_DEV,
        TaskStatus.BACKLOG,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.DONE: {TaskStatus.BACKLOG},
    TaskStatus.FAILED: {TaskStatus.BACKLOG, TaskStatus.CANCELLED},
    TaskStatus.CANCELLED: set(),
}
