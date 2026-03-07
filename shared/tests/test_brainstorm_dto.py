"""Unit tests for Brainstorm DTO — transition map completeness."""

from shared.contracts.dto.brainstorm import VALID_TRANSITIONS, BrainstormStatus


def test_every_status_has_transition_entry():
    for s in BrainstormStatus:
        assert s in VALID_TRANSITIONS, f"Missing transition entry for {s}"


def test_no_self_transitions():
    for from_s, to_set in VALID_TRANSITIONS.items():
        assert from_s not in to_set, f"Self-transition found: {from_s} -> {from_s}"


def test_draft_can_go_to_done():
    assert BrainstormStatus.DONE in VALID_TRANSITIONS[BrainstormStatus.DRAFT]


def test_done_can_go_to_triaged_or_archived():
    allowed = VALID_TRANSITIONS[BrainstormStatus.DONE]
    assert BrainstormStatus.TRIAGED in allowed
    assert BrainstormStatus.ARCHIVED in allowed


def test_triaged_can_go_to_archived():
    assert BrainstormStatus.ARCHIVED in VALID_TRANSITIONS[BrainstormStatus.TRIAGED]


def test_archived_is_terminal():
    assert VALID_TRANSITIONS[BrainstormStatus.ARCHIVED] == set()
