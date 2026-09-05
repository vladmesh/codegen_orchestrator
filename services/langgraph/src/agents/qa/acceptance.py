"""Prepare accumulated acceptance criteria for the central QA executor.

The central executor is deliberately not a second deployer or jobs-core client.
It may judge the product observable after a named fire, but deployment owns
privileged setting seed/readback and jobs core owns its transport response.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from shared.contracts.acceptance import parse_scheduled_behaviours

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


@dataclass(frozen=True)
class CriteriaAdjustment:
    """One platform-owned assertion omitted or made into a QA observable."""

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
    """Criteria the executor can grade plus the platform-owned adjustments."""

    criteria: str
    adjustments: tuple[CriteriaAdjustment, ...]


def prepare_central_qa_criteria(acceptance_criteria: str) -> PreparedCentralQACriteria:
    """Exclude platform proofs while retaining a criterion's product observable.

    Legacy checklist text may use Markdown bullets, prose before a path,
    backticks, or a versioned API prefix. A direct privileged settings or jobs
    transport assertion is omitted. If it has a ``THEN`` observable, it is
    rewritten instead: no observable is silently discarded. A valid ``FIRE
    JOB`` declaration is always retained because it is the contract that grants
    QA the named fire and its observable.
    """
    retained: list[str] = []
    adjustments: list[CriteriaAdjustment] = []
    for line in acceptance_criteria.splitlines():
        adjustment = _platform_owned_adjustment(line)
        if adjustment is None:
            retained.append(line)
            continue
        adjustments.append(adjustment)
        if adjustment.rewritten is not None:
            retained.append(adjustment.rewritten)
    return PreparedCentralQACriteria("\n".join(retained), tuple(adjustments))


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
