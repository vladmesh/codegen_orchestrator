"""Unit tests for Milestone DTO — transition map completeness."""

from shared.contracts.dto.milestone import VALID_TRANSITIONS, MilestoneStatus


def test_every_status_has_transition_entry():
    for s in MilestoneStatus:
        assert s in VALID_TRANSITIONS, f"Missing transition entry for {s}"


def test_no_self_transitions():
    for from_s, to_set in VALID_TRANSITIONS.items():
        assert from_s not in to_set, f"Self-transition found: {from_s} -> {from_s}"


def test_open_can_go_to_completed():
    assert MilestoneStatus.COMPLETED in VALID_TRANSITIONS[MilestoneStatus.OPEN]


def test_completed_is_terminal():
    assert VALID_TRANSITIONS[MilestoneStatus.COMPLETED] == set()
