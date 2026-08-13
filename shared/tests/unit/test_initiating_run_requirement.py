"""A project either names the run that initiated it, or owns no workers.

`initiating_run_id` is written once, by whoever starts the run, when the project
is created. The only rows without one are those written before the field
existed: their run was never recorded and cannot be reconstructed. These tests
pin the consequence — such a project is refused, never repaired with a
substitute — because a substitute would be stamped on a container as
`com.codegen.run.id` and would answer run-scoped label queries as if it were a
real run.
"""

import uuid

import pytest

from shared.contracts.dto.project import (
    ProjectCreate,
    ProjectPredatesRunOwnership,
    require_initiating_run,
)


class _Project:
    """The shape both the ORM row and `ProjectDTO` present to the rule."""

    def __init__(self, initiating_run_id: str | None):
        self.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        self.initiating_run_id = initiating_run_id


def test_a_project_that_names_its_run_hands_that_run_on_unchanged():
    assert require_initiating_run(_Project("live-20260813-abc")) == "live-20260813-abc"


@pytest.mark.parametrize("absent", [None, ""])
def test_a_project_without_a_run_is_refused_rather_than_given_one(absent):
    """The refusal is the point: no project id, no minted id, no constant.

    A legacy row's project id is the tempting substitute — it is right there and
    it is unique per project — but it is not a run, and every later run on that
    project would share it, so a query scoped to one run would select workers
    from another.
    """
    project = _Project(absent)

    with pytest.raises(ProjectPredatesRunOwnership) as raised:
        require_initiating_run(project)

    message = str(raised.value)
    assert str(project.id) in message
    assert str(project.id) != getattr(project, "initiating_run_id", None)
    # Nothing was written back: the rule reads, it does not repair.
    assert project.initiating_run_id == absent


def test_a_project_cannot_be_created_without_a_run():
    """The one writer is creation, so absence cannot be introduced from here on."""
    with pytest.raises(ValueError):
        ProjectCreate(title="t", initiating_run_id="")

    with pytest.raises(ValueError):
        ProjectCreate(title="t")
