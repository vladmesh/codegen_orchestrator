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

from pathlib import Path
import re
from types import SimpleNamespace

import httpx
import pipeline_helpers
import pytest
from run_intervention import (
    A_QUARANTINE,
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

#: The call shape every park uses to owe its owner a notice. Read out of the
#: tree so the event list above cannot go stale on its own schedule — the same
#: reason `db_teardown` derives its plan from `pg_constraint` rather than from a
#: list somebody maintained.
_OWED_PARK = re.compile(
    r"event=OwnerNotificationEvent\.([A-Z_]+)"
    r"(?:(?!terminal_status|\bowe_)[\s\S]){0,600}?"
    r"terminal_status=StoryStatus\.(WAITING_HUMAN_REVIEW|WAITING_USER_SECRET)"
)


def _events_owing_a_dod_terminal_status() -> set[str]:
    services = Path(__file__).resolve().parents[2] / "services"
    found: set[str] = set()
    for source in services.rglob("src/**/*.py"):
        for event, _status in _OWED_PARK.findall(source.read_text(encoding="utf-8")):
            found.add(OwnerNotificationEvent[event].value)
    return found


PROJECT = "11111111-1111-1111-1111-111111111111"
STORY = "story-1"


def _no_po_events(*_args, **_kwargs):
    """A PO stream with nothing on it, so the state source is what is tested."""
    return []


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
            A_QUARANTINE,
        }
        assert set(INTERVENTION_STATUSES) == {
            StoryStatus.WAITING_HUMAN_REVIEW.value,
            StoryStatus.WAITING_USER_SECRET.value,
        }

    def test_every_park_that_owes_a_dod_terminal_status_is_here(self):
        """The event list is checked against the producers, not remembered.

        Every park reaches its owner through `owe_owner_notification(...,
        terminal_status=StoryStatus.WAITING_*)`, so the events paired with one
        of the states the Definition of Done names are exactly the events this
        proof must recognise. The first version of the list missed
        `story_impossible_capacity` and `task_impossible_capacity`, both of
        which park a story in `waiting_human_review`; this reads the tree so the
        next one cannot be missed the same way.
        """
        found = _events_owing_a_dod_terminal_status()
        assert found, "no owner-notification park was found; this scan has gone stale"
        assert found <= set(INTERVENTION_EVENTS), sorted(found - set(INTERVENTION_EVENTS))


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


class TestTheStateSourceReadsARouteThatCarriesTheField:
    """`StoryRead` has no `owner_notification`, so the listing can never answer.

    The first version read it out of `GET /api/stories/?project_id=…`, whose
    response model is `list[StoryRead]`; FastAPI drops anything the model does
    not declare, so `story["owner_notification"]` was `{}` on every story and
    the third source asserted nothing while looking like a check. These drive
    the real helper against a fake transport, so the route it asks is part of
    the contract rather than a detail of one reading of the code.
    """

    @staticmethod
    def _ctx() -> dict:
        return {
            "project_id": PROJECT,
            "manifest": SimpleNamespace(run_id="run-1"),
            "run_po_input_cursor": "0-0",
        }

    @staticmethod
    def _client(handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))

    @staticmethod
    def _handler(notification: httpx.Response):
        asked: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.path)
            if request.url.path == "/api/stories/":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": STORY,
                            "status": StoryStatus.COMPLETED.value,
                            "quarantine_reason": None,
                        }
                    ],
                )
            return notification

        return handle, asked

    @pytest.mark.asyncio
    async def test_it_asks_the_per_story_owner_notification_route(self):
        handle, asked = self._handler(httpx.Response(404, json={"detail": "not found"}))
        ctx = self._ctx()
        async with self._client(handle) as api:
            await pipeline_helpers.record_no_intervention(api, ctx, command=_no_po_events)

        assert f"/api/stories/{STORY}/owner-notification" in asked
        assert ctx["no_intervention_error"] is None

    @pytest.mark.asyncio
    async def test_an_owed_park_notice_on_the_row_fails_the_run(self):
        """The case the history source cannot see: the publish never happened."""
        handle, _asked = self._handler(
            httpx.Response(
                200,
                json={
                    "event": OwnerNotificationEvent.STORY_BLOCKED.value,
                    "state": "owed",
                    "story_id": STORY,
                },
            )
        )
        ctx = self._ctx()
        async with self._client(handle) as api:
            await pipeline_helpers.record_no_intervention(api, ctx, command=_no_po_events)

        assert "waiting_human_review" in ctx["no_intervention_error"]

    @pytest.mark.asyncio
    async def test_an_unreadable_notification_is_unaskable_not_clean(self):
        handle, _asked = self._handler(httpx.Response(503, text="unavailable"))
        ctx = self._ctx()
        async with self._client(handle) as api:
            await pipeline_helpers.record_no_intervention(api, ctx, command=_no_po_events)

        state = next(
            check
            for check in ctx["no_intervention"]["checks"]
            if check["kind"] == "intervention_state"
        )
        assert state["outcome"] == "unaskable"
        assert ctx["no_intervention_error"]
