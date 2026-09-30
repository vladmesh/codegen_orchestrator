"""Prepare accumulated acceptance criteria for the central QA executor.

The central executor is deliberately not a second deployer or jobs-core client.
It may judge the product observable after a named fire, but deployment owns
privileged setting seed/readback and jobs core owns its transport response.

It is also not a writer. What QA can and never does is the capability
catalogue's (`shared.contracts.qa_capabilities`). A criterion that needs one of
the HTTP methods the catalogue's "never" entries forbid, on a product route, is
marked not verifiable here, before the executor exists, and never reaches it as
a check. Every other criterion reaches the executor, which performs it or
reports it with its own `qa_capability` cause.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from shared.contracts.acceptance import parse_scheduled_behaviours
from shared.contracts.qa_capabilities import http_write_methods

_SETTINGS_ASSERTION = re.compile(
    r"\bPOST\s+`?(?:/(?:api|v\d+))*/settings/(?:set|get)\b`?",
    re.IGNORECASE,
)
_JOBS_TRANSPORT_ASSERTION = re.compile(
    r"\bPOST\s+`?(?:/(?:api|v\d+))*/jobs/fire\b`?",
    re.IGNORECASE,
)
_THEN_OBSERVABLE = re.compile(r"\bTHEN\s+(?P<observable>\S.*)$", re.IGNORECASE)
_BULLET = re.compile(r"^(?P<bullet>\s*(?:[-*]|\d+[.)])\s+)")

# The rule for withholding a line: only when it certainly requires the tester to
# send an HTTP write. No other action is inferred here.
# When in doubt the line goes to the executor, whose own `qa_capability` cause
# is the safe fallback; a wrongly withheld line would fail every run of a
# correct product.
#
# An HTTP method is the line's action when it is an uppercase method token (not
# a slash-command or a path segment such as `/delete`) followed by a route:
# a path, optionally after a preposition, a scheme and host, or `localhost:port`.
# The first such token decides: a GET line is never withheld.
_HTTP_WRITES = http_write_methods()
_HTTP_READS = ("GET", "HEAD", "OPTIONS")
_METHOD_ROUTE = re.compile(
    rf"(?<![/\w-])(?P<method>{'|'.join((*_HTTP_READS, *sorted(_HTTP_WRITES)))})\s+"
    r"(?:(?:to|on|at|against)\s+)?`?(?:https?://[^\s/`]+|localhost:\d+)?/"
)

UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class CriteriaAdjustment:
    """One criterion line QA is not handed as written.

    ``dropped`` and ``rewritten`` are platform-owned assertions; ``unverifiable``
    is a line that needs an action outside QA's vocabulary.
    """

    action: str
    reason: str
    original: str
    rewritten: str | None = None

    def as_log(self) -> dict[str, str]:
        """Return bounded, structured postmortem evidence."""
        result = {
            "action": self.action,
            "reason": self.reason,
            "original": self.original,
        }
        if self.rewritten is not None:
            result["rewritten"] = self.rewritten
        return result


@dataclass(frozen=True)
class PreparedCentralQACriteria:
    """Criteria the executor can grade plus every line it was not handed."""

    criteria: str
    adjustments: tuple[CriteriaAdjustment, ...]

    @property
    def unverifiable(self) -> tuple[CriteriaAdjustment, ...]:
        """The lines QA had no tool for; each owes the run a `qa_capability` failure."""
        return tuple(a for a in self.adjustments if a.action == UNVERIFIABLE)


def prepare_central_qa_criteria(acceptance_criteria: str) -> PreparedCentralQACriteria:
    """Exclude platform proofs and unverifiable lines, keeping every observable.

    Legacy checklist text may use Markdown bullets, prose before a path,
    backticks, or a versioned API prefix. A direct privileged settings or jobs
    transport assertion is omitted. If it has a ``THEN`` observable, it is
    rewritten instead: no observable is silently discarded. A valid ``FIRE
    JOB`` declaration is always retained because it is the contract that grants
    QA the named fire and its observable. Any other line that needs an HTTP
    write is withheld and returned as ``unverifiable``, so the run reports it
    instead of losing it.
    """
    retained: list[str] = []
    adjustments: list[CriteriaAdjustment] = []
    for line in acceptance_criteria.splitlines():
        adjustment = _platform_owned_adjustment(line) or _unverifiable_adjustment(line)
        if adjustment is None:
            retained.append(line)
            continue
        adjustments.append(adjustment)
        if adjustment.rewritten is not None:
            retained.append(adjustment.rewritten)
    return PreparedCentralQACriteria("\n".join(retained), tuple(adjustments))


def _unverifiable_adjustment(line: str) -> CriteriaAdjustment | None:
    """Mark a line whose check needs an action none of QA's tools performs."""
    if parse_scheduled_behaviours(line):
        return None
    http = _METHOD_ROUTE.search(line)
    if http is None or http.group("method") not in _HTTP_WRITES:
        return None
    return CriteriaAdjustment(action=UNVERIFIABLE, reason="http_write", original=line)


def _platform_owned_adjustment(line: str) -> CriteriaAdjustment | None:
    """Classify one direct assertion, without claiming an output is a fact."""
    if parse_scheduled_behaviours(line):
        return None
    settings = _SETTINGS_ASSERTION.search(line)
    jobs = _JOBS_TRANSPORT_ASSERTION.search(line)
    if settings is None and jobs is None:
        return None

    reason = "settings_seed_readback" if settings is not None else "jobs_fire_transport"
    observable = _THEN_OBSERVABLE.search(line)
    if observable is None:
        return CriteriaAdjustment(action="dropped", reason=reason, original=line)

    bullet = _BULLET.match(line)
    prefix = bullet.group("bullet") if bullet else "- "
    subject = (
        "With the deployment-established setting, verify"
        if settings is not None
        else "After firing the named job, verify"
    )
    rewritten = f"{prefix}{subject}: {observable.group('observable').strip()}"
    return CriteriaAdjustment(
        action="rewritten",
        reason=reason,
        original=line,
        rewritten=rewritten,
    )
