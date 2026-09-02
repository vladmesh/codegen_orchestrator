"""The Story wait contract: one total mapping, and a field no client may patch."""

import pytest

from shared.contracts.dto.story import (
    VALID_TRANSITIONS,
    WAITING_ON_BY_STATUS,
    StoryStatus,
    StoryUpdate,
    StoryWaitingOn,
)


def test_every_status_declares_the_wait_it_implies():
    """Total over StoryStatus, so no transition can land on an undeclared wait."""
    assert set(WAITING_ON_BY_STATUS) == set(StoryStatus)


def test_declaring_the_wait_did_not_move_a_transition_edge():
    """`waiting_on` is derived from where a transition lands, never a new edge."""
    assert set(VALID_TRANSITIONS) == set(StoryStatus)
    assert VALID_TRANSITIONS[StoryStatus.ARCHIVED] == set()
    for status, allowed in VALID_TRANSITIONS.items():
        assert allowed <= set(StoryStatus), status


def test_the_waiting_statuses_map_to_their_own_wait():
    """The values a status implies, spelled out so a silent remap fails here."""
    assert WAITING_ON_BY_STATUS[StoryStatus.PR_REVIEW] is StoryWaitingOn.CI
    assert WAITING_ON_BY_STATUS[StoryStatus.DEPLOYING] is StoryWaitingOn.DEPLOY
    assert WAITING_ON_BY_STATUS[StoryStatus.TESTING] is StoryWaitingOn.QA
    assert WAITING_ON_BY_STATUS[StoryStatus.WAITING_HUMAN_REVIEW] is StoryWaitingOn.HUMAN_REVIEW
    assert WAITING_ON_BY_STATUS[StoryStatus.WAITING_USER_SECRET] is StoryWaitingOn.USER_SECRET


def test_resources_is_declared_and_no_story_status_implies_it():
    """Work parks for resources at the Task level, so no Story status carries it.

    The value exists because the wait is real and typed; nothing maps to it
    because no Story status is the one that means it.
    """
    assert StoryWaitingOn.RESOURCES in set(StoryWaitingOn)
    assert StoryWaitingOn.RESOURCES not in set(WAITING_ON_BY_STATUS.values())


@pytest.mark.parametrize("field", ["waiting_on", "status"])
def test_story_update_refuses_the_fields_a_transition_owns(field: str):
    """A poller cannot reach the field through the update contract it already imports."""
    with pytest.raises(ValueError, match="written by Story transitions only"):
        StoryUpdate.model_validate({field: "ci"})


def test_story_update_still_accepts_its_editorial_fields():
    """The refusal is scoped to the lifecycle fields and nothing else."""
    update = StoryUpdate.model_validate({"title": "renamed", "priority": 3})

    assert update.title == "renamed"
    assert update.priority == 3
