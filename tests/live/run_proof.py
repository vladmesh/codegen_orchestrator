"""A question asked of a live run, and the three answers it can have.

Every proof this run makes about itself — that a kind of resource is gone, that
no story ever waited for a person — is one question asked of one source. The
source can answer in three ways, and the whole point of this module is that the
third one is not the first one:

* **absent** — the question was asked and the answer names nothing.
* **leftover** — the question was asked and the answer names things.
* **unaskable** — the question could not be asked at all.

Card 1318 is why the third exists. An unreadable manager log was being rendered
as an empty log, so a read that never happened arrived at the assertion as a
clean result, and a run that proved nothing reported that it had. A source that
cannot answer is therefore never an empty answer here: `ask` turns *any*
exception out of a probe into `UNASKABLE`, and an unaskable check fails the
proof exactly like a leftover does, naming what could not be checked.

That rule only holds as far as the probes keep it. A probe that swallows its own
failure and returns `[]` is indistinguishable from a clean answer to anything
downstream, so every probe in `run_residue` and `run_intervention` raises on a
non-answer — a non-zero exit, a missing marker, an unparseable payload — rather
than returning one.

**A question nobody asked is not a passing question either.** `prove` is given
the kinds it must cover, and a kind with no check is reported as unasked and
fails, so a proof cannot shrink silently by losing a probe.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class ProofFailed(AssertionError):
    """A proof named a leftover, could not be taken, or was never taken."""


class ProofOutcome(StrEnum):
    ABSENT = "absent"
    LEFTOVER = "leftover"
    UNASKABLE = "unaskable"
    UNASKED = "unasked"


#: Said of a kind the proof declares and no check answered. Distinct from
#: `UNASKABLE`: nothing failed, nobody asked.
NEVER_ASKED = "this proof declares the kind and no check answered it"


@dataclass(frozen=True)
class ProofCheck:
    """One question, the source it was put to, and what came back.

    `question` is the question in the words the source was asked it in — a
    label query, a URL, a SQL predicate — because a reader of a red run needs to
    re-ask it by hand.
    """

    kind: str
    question: str
    outcome: ProofOutcome
    findings: tuple[str, ...] = ()
    unaskable_reason: str | None = None

    @property
    def clean(self) -> bool:
        return self.outcome is ProofOutcome.ABSENT

    def failure(self) -> str | None:
        """Why this check fails the proof, named, or None if it passed."""
        if self.outcome is ProofOutcome.ABSENT:
            return None
        if self.outcome is ProofOutcome.LEFTOVER:
            return f"{self.kind}: {', '.join(self.findings)} (asked: {self.question})"
        if self.outcome is ProofOutcome.UNASKED:
            return f"{self.kind}: {NEVER_ASKED}"
        return (
            f"{self.kind}: could not be checked: {self.unaskable_reason} (asked: {self.question})"
        )

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "question": self.question,
            "outcome": self.outcome.value,
            "findings": list(self.findings),
            "unaskable_reason": self.unaskable_reason,
        }


@dataclass(frozen=True)
class Proof:
    """Every check one proof is made of, and whether it holds."""

    subject: str
    checks: tuple[ProofCheck, ...] = ()
    # Free-form notes a reader needs to interpret the checks — which probe a
    # kind was answered by, why a kind is trivially absent on this route.
    notes: tuple[str, ...] = ()

    @property
    def failures(self) -> list[str]:
        return [failure for check in self.checks if (failure := check.failure()) is not None]

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "checks": [check.as_dict() for check in self.checks],
            "notes": list(self.notes),
        }

    def raise_if_unproven(self, headline: str) -> Proof:
        """Fail the run, naming every kind that is not proven absent."""
        if self.failures:
            raise ProofFailed(f"{headline} ({self.subject}): " + "; ".join(self.failures))
        return self


#: A probe names what it found, and raises when it could not look.
Probe = Callable[[], Iterable[str]]


@dataclass(frozen=True)
class Question:
    """One kind, the question to put, and the probe that puts it."""

    kind: str
    question: str
    probe: Probe
    field_note: str | None = field(default=None)


def ask(question: Question) -> ProofCheck:
    """Put one question to its source, and never read a failure as an absence."""
    try:
        findings = tuple(str(finding) for finding in question.probe())
    except BaseException as exc:  # noqa: BLE001 — every failure to ask is an unaskable check
        return ProofCheck(
            kind=question.kind,
            question=question.question,
            outcome=ProofOutcome.UNASKABLE,
            unaskable_reason=f"{type(exc).__name__}: {exc}",
        )
    if findings:
        return ProofCheck(
            kind=question.kind,
            question=question.question,
            outcome=ProofOutcome.LEFTOVER,
            findings=findings,
        )
    return ProofCheck(kind=question.kind, question=question.question, outcome=ProofOutcome.ABSENT)


def prove(
    subject: str,
    questions: Sequence[Question],
    *,
    required_kinds: Sequence[str],
    notes: Sequence[str] = (),
) -> Proof:
    """Ask every question, then report a declared kind nobody asked as unasked.

    Asking is total: one source that cannot answer does not stop the rest, so a
    red run names every kind it could not prove rather than the first one.
    """
    checks = [ask(question) for question in questions]
    asked = {check.kind for check in checks}
    checks += [
        ProofCheck(kind=kind, question=NEVER_ASKED, outcome=ProofOutcome.UNASKED)
        for kind in required_kinds
        if kind not in asked
    ]
    known = {question.kind: question.field_note for question in questions}
    return Proof(
        subject=subject,
        checks=tuple(sorted(checks, key=lambda check: check.kind)),
        notes=(*notes, *(note for note in known.values() if note)),
    )


def marker_payload(stdout: str, marker: str, *, subject: str) -> str:
    """The one line a probe container printed under its marker, or a raise.

    The marker is what tells a probe's answer apart from everything else a
    container writes to stdout. No marker means no answer — never an empty one.
    """
    for line in stdout.splitlines():
        if line.startswith(marker):
            return line[len(marker) :]
    raise ProofFailed(f"{subject} printed no {marker!r} payload")


def require_mapping(value: object, *, subject: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ProofFailed(f"{subject} answered with {type(value).__name__}, not an object")
    return value
