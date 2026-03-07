"""Brainstorm DTOs and enums — single source of truth for brainstorm statuses."""

from enum import StrEnum


class BrainstormStatus(StrEnum):
    DRAFT = "draft"
    DONE = "done"
    TRIAGED = "triaged"
    ARCHIVED = "archived"


# Valid status transitions: from_status -> set of allowed to_statuses
VALID_TRANSITIONS: dict[BrainstormStatus, set[BrainstormStatus]] = {
    BrainstormStatus.DRAFT: {BrainstormStatus.DONE},
    BrainstormStatus.DONE: {BrainstormStatus.TRIAGED, BrainstormStatus.ARCHIVED},
    BrainstormStatus.TRIAGED: {BrainstormStatus.ARCHIVED},
    BrainstormStatus.ARCHIVED: set(),
}
