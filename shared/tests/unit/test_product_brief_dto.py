"""The Product Brief vocabulary refuses the shapes the boundary cannot mean.

Fast, no database: what is checked here is that the types themselves make an
ambiguous disposition, a duplicate requirement id or a blank requirement
impossible to express. The durable behaviour — the idempotent admission, the
attempt fence — is proved against a real database in
`services/api/tests/service/test_product_brief_admission.py`.
"""

from pydantic import ValidationError
import pytest

from shared.contracts.dto.engineering_dispatch import (
    OVERRIDABLE_REFUSALS,
    EngineeringDispatchRefusal,
)
from shared.contracts.dto.product_brief import (
    ProductBriefAdmissionOutcome,
    ProductBriefAdmissionRead,
    ProductBriefContent,
    RequirementCoverageCreate,
)


def _content(**overrides) -> dict:
    base = {
        "summary": "A bot that tracks reading",
        "must_requirements": [{"id": "r1", "text": "It stores a book"}],
    }
    base.update(overrides)
    return base


class TestProductBriefContent:
    def test_minimal(self):
        content = ProductBriefContent.model_validate(_content())
        assert [r.id for r in content.must_requirements] == ["r1"]

    def test_a_brief_with_no_must_requirement_is_not_a_brief(self):
        """Coverage over an empty requirement set would admit vacuously."""
        with pytest.raises(ValidationError):
            ProductBriefContent.model_validate(_content(must_requirements=[]))

    def test_duplicate_requirement_ids_are_refused(self):
        """Requirements are addressed by id, so two rows cannot share one."""
        with pytest.raises(ValidationError):
            ProductBriefContent.model_validate(
                _content(
                    must_requirements=[
                        {"id": "r1", "text": "one"},
                        {"id": "r1", "text": "two"},
                    ]
                )
            )

    def test_a_blank_requirement_is_refused(self):
        with pytest.raises(ValidationError):
            ProductBriefContent.model_validate(
                _content(must_requirements=[{"id": "  ", "text": "one"}])
            )

    def test_unknown_content_fields_are_refused(self):
        """Confirmed content is hashed, so a silently dropped field is a lie."""
        with pytest.raises(ValidationError):
            ProductBriefContent.model_validate(_content(extra="smuggled"))


class TestRequirementCoverageCreate:
    def test_a_covering_task_is_a_disposition(self):
        coverage = RequirementCoverageCreate(
            requirement_id="r1", planning_attempt_id="plan-1", task_id="task-1"
        )
        assert coverage.task_id == "task-1"

    def test_a_returned_reason_is_a_disposition(self):
        coverage = RequirementCoverageCreate(
            requirement_id="r1", planning_attempt_id="plan-1", returned_reason="out of scope"
        )
        assert coverage.returned_reason == "out of scope"

    def test_no_disposition_is_refused(self):
        with pytest.raises(ValidationError):
            RequirementCoverageCreate(requirement_id="r1", planning_attempt_id="plan-1")

    def test_two_dispositions_are_refused(self):
        """Covered *and* returned is two answers to one question."""
        with pytest.raises(ValidationError):
            RequirementCoverageCreate(
                requirement_id="r1",
                planning_attempt_id="plan-1",
                task_id="task-1",
                returned_reason="out of scope",
            )

    def test_coverage_names_the_attempt_that_wrote_it(self):
        """No default: a disposition that named no attempt could not be superseded."""
        with pytest.raises(ValidationError):
            RequirementCoverageCreate(requirement_id="r1", task_id="task-1")


class TestProductBriefAdmissionRead:
    def test_an_incomplete_admission_releases_nothing(self):
        answer = ProductBriefAdmissionRead(
            brief_id="brief-1",
            story_id="story-1",
            outcome=ProductBriefAdmissionOutcome.INCOMPLETE,
            missing_requirement_ids=["r2"],
        )
        assert answer.released_task_ids == []


def test_the_brief_refusal_is_not_overridable():
    """An operator may not buy a worker for a plan the architect has not finished.

    Every other rung-1 refusal is a judgement about one task an operator can be
    authorised to make. This one is a property of the whole plan: the tasks are
    released together, by the brief's admission step, or not at all.
    """
    assert EngineeringDispatchRefusal.PRODUCT_BRIEF_NOT_ADMITTED not in OVERRIDABLE_REFUSALS
