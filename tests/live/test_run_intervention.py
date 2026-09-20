"""Offline regressions for the run's zero-intervention proof.

The property under test is "ever", not "now": the case that matters is a story
that entered `waiting_human_review` or a quarantine, was recovered, and ended
`completed` with `quarantine_reason` cleared. Reading the terminal state passes
that run; reading the durable PO history fails it, which is what the Definition
of Done asks for.

And the same rule as everywhere else in this proof: a history that could not be
read is not a history of no interventions.
"""

from __future__ import annotations

import pytest
from run_intervention import (
    INTERVENTION_EVENTS,
    INTERVENTION_KINDS,
    INTERVENTION_STATUSES,
    InterventionOps,
    RunStories,
    interventions_in_history,
    interventions_in_state,
    prove_no_intervention,
)
from run_proof import ProofOutcome

from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent

pytestmark = pytest.mark.needs_no_api_credential

PROJECT = "11111111-1111-1111-1111-111111111111"
STORY = "story-1"
STORIES = RunStories(project_id=PROJECT, story_ids=(STORY,))


def event(name: str, **fields) -> dict:
    return {
        "type": "system_event",
        "event": name,
        "text": "",
        "story_id": STORY,
        "project_id": PROJECT,
        "timestamp": "2026-09-20T00:00:00+00:00",
        **fields,
    }


def completed_story(**fields) -> dict:
    return {
        "id": STORY,
        "status": StoryStatus.COMPLETED.value,
        "quarantine_reason": None,
        "owner_notification": {"event": OwnerNotificationEvent.STORY_COMPLETED.value},
        **fields,
    }


def prove(history, state) -> object:
    return prove_no_intervention(
        InterventionOps(history=lambda: history, state=lambda: state),
        STORIES,
        subject="run 1",
    )


def outcome(proof, kind: str) -> ProofOutcome:
    return next(check.outcome for check in proof.checks if check.kind == kind)


class TestEverNotNow:
    def test_a_park_that_was_recovered_still_fails_the_run(self):
        """The whole point: the story ends `completed` and the run is still red."""
        proof = prove([event(OwnerNotificationEvent.STORY_BLOCKED.value)], [completed_story()])
        assert outcome(proof, "intervention_history") is ProofOutcome.LEFTOVER
        assert outcome(proof, "intervention_state") is ProofOutcome.ABSENT
        assert proof.failures == [
            "intervention_history: story story-1 entered waiting_human_review "
            "(story_blocked at 2026-09-20T00:00:00+00:00) (asked: "
            + next(check.question for check in proof.checks if check.kind == "intervention_history")
            + ")"
        ]

    def test_a_quarantine_that_was_cleared_still_fails_the_run(self):
        proof = prove([event(OwnerNotificationEvent.STORY_QUARANTINED.value)], [completed_story()])
        assert outcome(proof, "intervention_history") is ProofOutcome.LEFTOVER
        assert "a quarantine" in "; ".join(proof.failures)

    def test_a_secret_wait_that_was_resolved_still_fails_the_run(self):
        proof = prove(
            [event(OwnerNotificationEvent.STORY_WAITING_USER_SECRET.value)], [completed_story()]
        )
        assert "waiting_user_secret" in "; ".join(proof.failures)

    def test_a_run_that_never_parked_is_clean(self):
        proof = prove([event(OwnerNotificationEvent.STORY_COMPLETED.value)], [completed_story()])
        assert proof.failures == []
        assert sorted(check.kind for check in proof.checks) == sorted(INTERVENTION_KINDS)

    def test_the_three_states_the_definition_of_done_names_are_the_ones_asked_about(self):
        assert set(INTERVENTION_EVENTS.values()) == {
            StoryStatus.WAITING_HUMAN_REVIEW.value,
            StoryStatus.WAITING_USER_SECRET.value,
            "a quarantine",
        }
        assert set(INTERVENTION_STATUSES) == {
            StoryStatus.WAITING_HUMAN_REVIEW.value,
            StoryStatus.WAITING_USER_SECRET.value,
        }


class TestTheHistoryIsThisRunsAlone:
    def test_another_projects_park_is_not_this_runs(self):
        other = event(
            OwnerNotificationEvent.STORY_BLOCKED.value,
            story_id="story-other",
            project_id="22222222-2222-2222-2222-222222222222",
        )
        assert interventions_in_history([other], STORIES) == []

    def test_a_park_naming_only_this_runs_project_is_this_runs(self):
        orphan = event(OwnerNotificationEvent.STORY_BLOCKED.value, story_id="")
        assert len(interventions_in_history([orphan], STORIES)) == 1


class TestTheStateIsTheSecondSource:
    def test_a_story_still_parked_fails_even_with_no_history_entry(self):
        proof = prove([], [completed_story(status=StoryStatus.WAITING_HUMAN_REVIEW.value)])
        assert outcome(proof, "intervention_history") is ProofOutcome.ABSENT
        assert outcome(proof, "intervention_state") is ProofOutcome.LEFTOVER

    def test_a_story_still_carrying_quarantine_evidence_fails(self):
        found = interventions_in_state([completed_story(quarantine_reason={"qa": "red"})])
        assert found == ["story story-1 still carries a quarantine_reason"]

    def test_an_unpublished_park_notice_on_the_row_fails(self):
        found = interventions_in_state(
            [
                completed_story(
                    owner_notification={"event": OwnerNotificationEvent.STORY_BLOCKED.value}
                )
            ]
        )
        assert found == [
            "story story-1 holds an unpublished notice that it entered waiting_human_review"
        ]


class TestASourceThatCouldNotAnswer:
    def test_an_unreadable_history_is_unaskable_not_clean(self):
        def unreadable():
            raise RuntimeError("redis refused XRANGE")

        proof = prove_no_intervention(
            InterventionOps(history=unreadable, state=lambda: [completed_story()]),
            STORIES,
            subject="run 1",
        )
        check = next(c for c in proof.checks if c.kind == "intervention_history")
        assert check.outcome is ProofOutcome.UNASKABLE
        assert "redis refused XRANGE" in check.unaskable_reason
        assert proof.failures

    def test_an_unreadable_state_is_unaskable_not_clean(self):
        def unreadable():
            raise RuntimeError("GET /api/stories/ answered 503")

        proof = prove_no_intervention(
            InterventionOps(history=lambda: [], state=unreadable),
            STORIES,
            subject="run 1",
        )
        assert outcome(proof, "intervention_state") is ProofOutcome.UNASKABLE
        assert proof.failures
