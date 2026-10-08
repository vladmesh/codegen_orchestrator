"""Only a fully returned, taskless plan is a recoverable planning stop."""

import pytest

from shared.contracts.dto.story_failure import StoryFailureCode
from shared.models import ProductBrief, RequirementCoverage, Task
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
    failure = returned_plan_failure(brief, rows, tasks=[])
    assert failure.code is StoryFailureCode.PLANNING_FAILED
    assert failure.source == "architect"
    assert "save: catalog_install refused: invalid_binding" in failure.detail
    assert "list: catalog_install refused: invalid_binding" in failure.detail


def test_voided_work_from_a_superseded_attempt_does_not_hide_the_returned_plan():
    brief, rows = plan()
    voided = Task(id="task-old", planning_attempt_id="plan-old", status="cancelled")
    failure = returned_plan_failure(brief, rows, tasks=[voided])
    assert failure.code is StoryFailureCode.PLANNING_FAILED


def test_current_attempt_work_keeps_a_fully_returned_plan_from_parking():
    brief, rows = plan()
    task = Task(id="task-current", planning_attempt_id=brief.planning_attempt_id, status="todo")
    assert returned_plan_failure(brief, rows, tasks=[task]) is None


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
    tasks = [Task(planning_attempt_id=brief.planning_attempt_id)] if case == "task" else []
    assert returned_plan_failure(brief, rows, tasks=tasks) is None
