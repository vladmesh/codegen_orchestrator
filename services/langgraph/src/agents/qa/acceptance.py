"""Prepare accumulated acceptance criteria for the central QA executor.

The central executor is deliberately not a second deployer or jobs-core client.
It may judge the product observable after a named fire, but deployment owns
privileged setting seed/readback and jobs core owns its transport response.

It is also not a writer. Its whole vocabulary is a read-only HTTP GET, a
Telegram text message, an inline button press and a declared ``FIRE JOB``. A
criterion that needs any other action — an HTTP write on a product route, a
photo or file sent to the bot — is marked not verifiable here, before the
executor exists, and never reaches it as a check.
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

# An HTTP method QA has no tool for, applied to a route.
_HTTP_WRITE = re.compile(r"\b(?:POST|PUT|PATCH|DELETE)\s+`?/", re.IGNORECASE)
# A word, not a route segment: `/uploads/photos` names a path QA may GET.
_WORD = r"(?<![/\w])"
_MEDIA = (
    rf"{_WORD}(?:photos?|images?|pictures?|screenshots?|files?|documents?|videos?|voice|audio"
    r"|media)\b"
)
_UPLOAD_VERB = rf"{_WORD}(?:upload|attach)\w*"
# Media the tester would have to send: an upload or attach verb, a media item
# sent *to the bot*, or a media item used as an input ("receipt photo → OCR").
# A bot replying with media is evidence QA can read, and is not matched.
_TELEGRAM_UPLOAD = (
    re.compile(rf"{_UPLOAD_VERB}.*{_MEDIA}", re.IGNORECASE),
    re.compile(rf"{_MEDIA}.*{_UPLOAD_VERB}", re.IGNORECASE),
    re.compile(rf"\bsend\w*\s+(?:\w+\s+){{0,3}}{_MEDIA}.*\bto\s+the\s+bot\b", re.IGNORECASE),
    re.compile(rf"{_MEDIA}\s*(?:→|->|=>)", re.IGNORECASE),
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
    write or a Telegram media upload is withheld and returned as
    ``unverifiable``, so the run reports it instead of losing it.
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
    if _HTTP_WRITE.search(line):
        reason = "http_write"
    elif any(pattern.search(line) for pattern in _TELEGRAM_UPLOAD):
        reason = "telegram_media_upload"
    else:
        return None
    return CriteriaAdjustment(action=UNVERIFIABLE, reason=reason, original=line)


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
