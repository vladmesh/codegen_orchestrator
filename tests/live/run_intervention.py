"""Prove no story of a run ever needed a person, from history rather than state.

The Definition of Done asks that no story of the run *ever* entered
`waiting_human_review`, `waiting_user_secret` or a quarantine. "Ever" is the
whole of it: a story that parked for a human and was then recovered — by the
supervisor, by an operator acceptance, by a retry — ends `completed` like any
other, and a run that only read the terminal status would report it as a run
that never needed anybody.

The terminal state cannot answer the question, and neither can the story row.
`quarantine_reason` is the row that says a story was quarantined, and
`services/api/src/routers/stories.py` clears it on recovery (`story.
quarantine_reason = None`), which is correct for the product and fatal for this
assertion. `waiting_on` is the same: it is written by the transition that lands
on a status, so it says what the story is waiting for now and nothing about what
it waited for an hour ago.

**What does survive a recovery is the notice.** Every park publishes an owner
notification onto `po:input` — `STORY_BLOCKED` for `waiting_human_review`
including the infrastructure park, `STORY_WAITING_USER_SECRET`, and
`STORY_QUARANTINED` — and that stream entry is not rewritten when the story
moves on. Reading the stream from a cursor taken before the run began is
therefore the run's durable history: it names every moment the platform asked
its owner for something, whether or not the ask was later answered by the
platform itself.

**The state is read too, as the second source and not the first.** A park whose
notification could not be published leaves no history entry, so the current
status, the `quarantine_reason` and the *owed* `owner_notification` of each of
the run's stories are asked as well. Either source naming a story fails the run.

The notification is read through `GET /api/stories/{id}/owner-notification` and
not out of the story listing, because `StoryRead` — the list route's response
model — has no `owner_notification` field, so FastAPI drops it and the read
would be `{}` on every story forever. That was the second half of the same
defect as the PO thread id: a source that looks like a check and asserts
nothing. `services/api/src/infrastructure_park.py` writes that record in state
`OWED` and leaves delivery to a later publisher, which is exactly the park the
history source cannot see.

**And a source that cannot answer is not a source that said no.** Both reads go
through `run_proof.ask`, so a Redis that refused the range or an API that would
not answer is a red run naming what could not be checked — never a quiet pass.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from run_proof import Proof, Question, prove

from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent

#: The two things this proof asks, and the two sources it asks them of.
INTERVENTION_KINDS = ("intervention_history", "intervention_state")

#: A quarantine has no status of its own: it is a `quarantine_reason` on a story
#: the platform also parks for a person.
A_QUARANTINE = "a quarantine"

#: The owner notifications that mean the platform stopped and asked a person.
#: Keyed by event, valued by the thing the Definition of Done calls it, so a red
#: run says "waiting_human_review", which is the vocabulary the card is in.
#:
#: This is not a list somebody remembered. Every entry is an event whose
#: producer publishes it while landing a story in one of the states the
#: Definition of Done names, and
#: `test_run_intervention.py::test_every_park_that_owes_a_dod_terminal_status_is_here`
#: reads `services/` for `terminal_status=StoryStatus.WAITING_*` and fails when
#: a producer appears that is not here. `story_impossible_capacity` and
#: `task_impossible_capacity` are in the set because of that scan:
#: `supervisor/deploy.py` and `supervisor/liveness.py` both park the story in
#: `waiting_human_review` under them, and the first version of this list missed
#: both.
INTERVENTION_EVENTS: Mapping[str, str] = {
    OwnerNotificationEvent.STORY_BLOCKED.value: StoryStatus.WAITING_HUMAN_REVIEW.value,
    OwnerNotificationEvent.STORY_QUARANTINED.value: A_QUARANTINE,
    OwnerNotificationEvent.STORY_IMPOSSIBLE_CAPACITY.value: (
        StoryStatus.WAITING_HUMAN_REVIEW.value
    ),
    OwnerNotificationEvent.TASK_IMPOSSIBLE_CAPACITY.value: (StoryStatus.WAITING_HUMAN_REVIEW.value),
    OwnerNotificationEvent.STORY_WAITING_USER_SECRET.value: StoryStatus.WAITING_USER_SECRET.value,
}

#: The statuses the Definition of Done names, for the second source. A story
#: sitting in one of them right now fails the run even if its notification
#: never reached the stream.
INTERVENTION_STATUSES: Mapping[str, str] = {
    StoryStatus.WAITING_HUMAN_REVIEW.value: StoryStatus.WAITING_HUMAN_REVIEW.value,
    StoryStatus.WAITING_USER_SECRET.value: StoryStatus.WAITING_USER_SECRET.value,
}


@dataclass(frozen=True)
class RunStories:
    """Which stories and project one run's history is read for.

    A PO stream is shared by every run on the contour, so an entry only belongs
    to this run when it names this run's project or one of its stories. An entry
    that names neither is somebody else's park and is left alone.
    """

    project_id: str = ""
    story_ids: tuple[str, ...] = ()

    def owns(self, event: Mapping) -> bool:
        story_id = str(event.get("story_id") or "")
        project_id = str(event.get("project_id") or "")
        return (bool(story_id) and story_id in self.story_ids) or (
            bool(project_id) and project_id == self.project_id
        )


def interventions_in_history(events: Iterable[Mapping], stories: RunStories) -> list[str]:
    """Every park this run's PO history records, named as the card names it."""
    found: list[str] = []
    for event in events:
        name = str(event.get("event") or "")
        meaning = INTERVENTION_EVENTS.get(name)
        if meaning is None or not stories.owns(event):
            continue
        subject = event.get("story_id") or event.get("project_id")
        found.append(f"story {subject} entered {meaning} ({name} at {event.get('timestamp')})")
    return found


def interventions_in_state(stories: Iterable[Mapping]) -> list[str]:
    """Every story still parked, still quarantined, or still owing a park notice.

    Three reads of one row, because a park writes three things and a lost
    notification only costs the history entry. The status is the park itself;
    `quarantine_reason` is the QA evidence a quarantine leaves; and
    `owner_notification` is the notice the park owed — which is still on the row
    when the publish never happened, and is exactly the case the history source
    cannot see.

    `owner_notification` has to be put there by the caller from the story's own
    internal route: the list route's response model does not carry it. A caller
    that cannot read it must raise rather than pass a story without it, or this
    becomes the third check that asserts nothing again.
    """
    found: list[str] = []
    for story in stories:
        story_id = story.get("id")
        meaning = INTERVENTION_STATUSES.get(str(story.get("status") or ""))
        if meaning is not None:
            found.append(f"story {story_id} is in {meaning}")
        if story.get("quarantine_reason"):
            found.append(f"story {story_id} still carries a quarantine_reason")
        notification = story.get("owner_notification") or {}
        owed = INTERVENTION_EVENTS.get(str(notification.get("event") or ""))
        if owed is not None:
            found.append(f"story {story_id} holds an unpublished notice that it entered {owed}")
    return found


@dataclass(frozen=True)
class InterventionOps:
    """The two durable reads this proof makes, each raising when it cannot read.

    `history` answers with the PO entries published since the run's own cursor;
    `state` answers with this run's stories as the API has them now.
    """

    history: Callable[[], Iterable[Mapping]]
    state: Callable[[], Iterable[Mapping]]


def prove_no_intervention(
    ops: InterventionOps,
    stories: RunStories,
    *,
    subject: str,
    notes: Sequence[str] = (),
) -> Proof:
    """Ask both sources; either one naming a story fails the run."""
    return prove(
        subject,
        [
            Question(
                kind="intervention_history",
                question=(
                    f"po:input entries since this run's cursor naming project {stories.project_id} "
                    f"or stories {list(stories.story_ids)}, filtered to "
                    f"{sorted(INTERVENTION_EVENTS)}"
                ),
                probe=lambda: interventions_in_history(ops.history(), stories),
            ),
            Question(
                kind="intervention_state",
                question=(
                    f"GET /api/stories/?project_id={stories.project_id}: status in "
                    f"{sorted(INTERVENTION_STATUSES)}, a quarantine_reason, or an "
                    "owner_notification of a park"
                ),
                probe=lambda: interventions_in_state(ops.state()),
            ),
        ],
        required_kinds=INTERVENTION_KINDS,
        notes=notes,
    )
