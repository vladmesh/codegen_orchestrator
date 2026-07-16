"""Acceptance criteria — the contract QA validates a deployed story against.

`Repository.acceptance_criteria` is the single source of truth: it holds the
accumulated regression checklist for the project, seeded when the repository is
created and extended by the architect as stories add functionality. Story and
task criteria describe work to be done; they are not what QA runs.

Format is one check per line, starting with "- ". Checks that state a plain GET
expectation are machine-checkable, so QA runs them itself over HTTP instead of
handing the criteria to a coding agent.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

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
