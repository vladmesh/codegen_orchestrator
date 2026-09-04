"""Test-only canonical project-deletion order assertions."""

from typing import Final

PROJECT_BRIEF_TASK_STORY_DELETE_ORDER: Final[tuple[str, ...]] = (
    "requirement_coverages",
    "product_briefs",
    "tasks",
    "stories",
)
