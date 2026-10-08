"""Only a fully returned, taskless plan is a recoverable planning stop."""

import pytest

from shared.contracts.dto.story_failure import StoryFailureCode
from shared.models import ProductBrief, RequirementCoverage
from src.routers._product_brief_helpers import returned_plan_failure


def plan():
    brief = ProductBrief(
        id="brief-notes",
        planning_attempt_id="plan-notes",
        content={"must_requirements": [{"id": "save"}, {"id": "list"}]},
    )
    rows = [
        RequirementCoverage(
            requirement_id=key,
            planning_attempt_id="plan-notes",
            returned_reason="catalog_install refused: invalid_binding",
        )
        for key in ("save", "list")
    ]
    return brief, rows


def test_a_taskless_fully_returned_plan_carries_its_refusal():
    brief, rows = plan()
    failure = returned_plan_failure(brief, rows, has_tasks=False)
    assert failure.code is StoryFailureCode.PLANNING_FAILED
    assert failure.source == "architect"
    assert "save: catalog_install refused: invalid_binding" in failure.detail
    assert "list: catalog_install refused: invalid_binding" in failure.detail


@pytest.mark.parametrize("case", ["task", "missing", "covered", "stale", "empty"])
def test_other_plans_are_not_classified_as_fully_returned(case):
    brief, rows = plan()
    if case == "missing":
        rows.pop()
    elif case == "covered":
        rows[0].task_id = "task-save"
        rows[0].returned_reason = None
    elif case == "stale":
        rows[0].planning_attempt_id = "plan-stale"
    elif case == "empty":
        brief.content = {"must_requirements": []}
    assert returned_plan_failure(brief, rows, has_tasks=case == "task") is None
