"""Acceptance criteria — the contract QA validates a deployed story against.

`Repository.acceptance_criteria` is the single source of truth: it holds the
accumulated regression checklist for the project, seeded when the repository is
created and extended by the architect as stories add functionality. Story and
task criteria describe work to be done; they are not what QA runs.

Format is one check per line, starting with "- ". Checks that state a plain GET
expectation are machine-checkable, so QA runs them itself over HTTP instead of
handing the criteria to a coding agent.

Two kinds of line are read by the platform rather than by the executor. A plain
GET expectation is one; a `FIRE JOB <name> THEN <observable>` line is the other,
and it is where the *name* of a scheduled behaviour comes from. QA never invents
a behaviour name and never derives one from prose: it fires only what a
checklist line named, and it judges the run on the observable that line states,
never on the product core's record of having dispatched the fire.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Every repository starts with the one check that holds for any deployed
#: service. Seeded at repository creation so QA has criteria for a story that
#: never went through the architect.
BASELINE_ACCEPTANCE_CRITERIA = "- GET /health returns 200"

_HEALTH_CHECK_RE = re.compile(
    r"^-\s*GET\s+(?P<path>/\S*)\s+returns\s+(?P<expected_status>\d{3})$",
    re.IGNORECASE,
)


class HealthCriterion(BaseModel):
    """A GET check QA can verify without an LLM."""

    path: str
    expected_status: int


def parse_health_only_criteria(criteria: str) -> list[HealthCriterion] | None:
    """Parse criteria into GET checks, or None if any line needs an LLM.

    Returns None unless *every* check is a plain GET expectation — a criteria
    block with one prose line is not something HTTP calls can decide.
    """
    checks = []
    for line in criteria.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _HEALTH_CHECK_RE.match(stripped)
        if not match:
            return None
        checks.append(
            HealthCriterion(
                path=match.group("path"),
                expected_status=int(match.group("expected_status")),
            )
        )
    return checks or None


#: How a checklist names a scheduled behaviour QA must invoke, and what proves
#: it ran. The behaviour's name is read off this line deterministically — the
#: executor never guesses it, and nothing infers it from the prose around it:
#:
#:     - FIRE JOB daily_digest THEN the bot sends today's digest to the owner
#:     - FIRE JOB daily_digest WITH {"chat_id": 42} THEN the bot sends ...
#:
#: `WITH` carries the arguments the product's declared `jobs_schema` must
#: accept, as one JSON object. A line whose arguments are not a JSON object is
#: not a behaviour criterion at all: it stays in the checklist as prose, and no
#: fire is offered for it, because a fire the platform cannot spell exactly is
#: a fire nobody may make.
_SCHEDULED_BEHAVIOUR_RE = re.compile(
    r"^-\s*FIRE\s+JOB\s+(?P<name>[A-Za-z][A-Za-z0-9_.-]*)"
    r"(?:\s+WITH\s+(?P<arguments>\{.*\}))?"
    r"\s+THEN\s+(?P<observable>\S.*)$",
    re.IGNORECASE,
)


class ScheduledBehaviourCriterion(BaseModel):
    """One named scheduled behaviour of the product, and the observable it owes.

    `name` and `arguments` are what a fire carries; `observable` is what the
    verdict rests on. They are separate fields because they are judged in
    different places: the product's core answers the fire, and only the
    product's own output answers the observable. A recorded dispatch is never
    the answer to `observable` — the core publishes an event, and publishing it
    says nothing about whether any provider consumed it or ran the behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    observable: str = Field(min_length=1)


def parse_scheduled_behaviours(criteria: str) -> list[ScheduledBehaviourCriterion]:
    """Read every scheduled behaviour this run's checklist names, in order.

    A name appearing twice is one behaviour: the first line naming it wins, and
    a later line naming it again describes the same single execution rather
    than a second one. That is the same rule the run's command identity
    encodes, stated once here so the two cannot disagree.
    """
    behaviours: list[ScheduledBehaviourCriterion] = []
    seen: set[str] = set()
    for line in criteria.splitlines():
        match = _SCHEDULED_BEHAVIOUR_RE.match(line.strip())
        if not match:
            continue
        raw_arguments = match.group("arguments")
        arguments: object = {}
        if raw_arguments is not None:
            try:
                arguments = json.loads(raw_arguments)
            except ValueError:
                continue
            if not isinstance(arguments, dict):
                continue
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)
        behaviours.append(
            ScheduledBehaviourCriterion(
                name=name,
                arguments=arguments,
                observable=match.group("observable").strip(),
            )
        )
    return behaviours
