"""The one QA target profile, and the receipt a managed target carries for it.

A managed deploy target is only useful to QA if the artefacts the `qa_identity`
role installs are the ones the runtime speaks to: the account, its one sudo rule
and above all `/usr/local/bin/qa-docker` with the verbs the runner calls. A host
provisioned before a verb existed passes every presence check — the account is
there, `qa_ssh_user` is written, `provisioning_phase` is `complete` — and still
refuses `read-contract` halfway through a paid run. The pilot canary of
2026-09-12 turned exactly that into an engineering fix task.

So readiness is a version, not a presence claim:

* :data:`QA_TARGET_PROFILE_VERSION` is derived from the role's files by
  :func:`qa_target_artefact_digest`, with the two lines that carry the version
  blanked. A unit test holds the constant to the files, so any change to a role
  artefact changes the version that proves it.
* the wrapper answers `qa-docker version` with that value, and the role's proof
  refuses a host whose wrapper, reached through the account's own sudo rule,
  answers anything else.
* reconciliation records the proved version and time on the server row, through
  a dedicated API boundary; admission and QA refuse a managed target whose
  receipt is missing or names another version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
from pathlib import Path
import re

from shared.contracts.dto.server import ServerDTO

#: The profile the repository's `qa_identity` role installs and proves. Derived
#: from the role files; see :func:`qa_target_artefact_digest`.
QA_TARGET_PROFILE_VERSION = "064dbee1a73dada3"
QA_TARGET_PROFILE_VERSION_LENGTH = 16

#: What the runtime needs the wrapper to answer. `version` itself is one of them:
#: a wrapper that cannot say what it is cannot be told apart from an old one.
QA_DOCKER_REQUIRED_VERBS = frozenset(
    {"diff", "inspect", "logs", "port", "ps", "read-contract", "stats", "top", "version"}
)

_WRAPPER_VERSION_LINE = re.compile(r"^QA_TARGET_PROFILE_VERSION=.*$", re.MULTILINE)
_DEFAULTS_VERSION_LINE = re.compile(r"^qa_target_profile_version:.*$", re.MULTILINE)
_WRAPPER_ANSWER = re.compile(r"^qa-docker profile=(?P<version>\S+) verbs=(?P<verbs>.+)$")
_PROOF_VERSION = re.compile(r"qa_target_version=(?P<version>[0-9a-f]{16})\b")


def qa_target_artefact_digest(role_dir: Path) -> str:
    """The profile version the files of one `qa_identity` role directory define.

    Every regular file under the role counts, in a stable order, keyed by its
    path inside the role. The wrapper's and the defaults' version lines are
    blanked first, because they carry the result: the version is what the rest
    of the role says, not what those two lines claim.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in role_dir.rglob("*") if p.is_file()):
        relative = path.relative_to(role_dir).as_posix()
        if "__pycache__" in relative:
            continue
        content = path.read_text()
        content = _WRAPPER_VERSION_LINE.sub("QA_TARGET_PROFILE_VERSION=", content)
        content = _DEFAULTS_VERSION_LINE.sub("qa_target_profile_version:", content)
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content.encode())
        digest.update(b"\0")
    return digest.hexdigest()[:QA_TARGET_PROFILE_VERSION_LENGTH]


class QATargetReceiptRejection(StrEnum):
    """Why a managed target's readiness receipt does not admit it."""

    # No successful role proof was ever recorded for this row.
    MISSING = "qa_target_receipt_missing"
    # A proof was recorded for another profile: the role changed since.
    STALE = "qa_target_receipt_stale"


def qa_target_receipt_rejection(server: ServerDTO) -> QATargetReceiptRejection | None:
    """Return why this server's receipt does not prove the current profile."""
    if server.qa_target_version is None or server.qa_target_proved_at is None:
        return QATargetReceiptRejection.MISSING
    if server.qa_target_version != QA_TARGET_PROFILE_VERSION:
        return QATargetReceiptRejection.STALE
    return None


def wrapper_answer_problem(answer: str) -> str | None:
    """What is wrong with a live `qa-docker version` answer, or ``None``.

    The answer is compared whole — profile and verbs — because a wrapper that
    names the right profile and lacks a verb is not one this runtime can use.
    """
    lines = [line for line in answer.strip().splitlines() if line.strip()]
    match = _WRAPPER_ANSWER.match(lines[-1].strip()) if lines else None
    if match is None:
        return f"the wrapper gave no profile answer: [{answer.strip()[:300]}]"
    version = match.group("version")
    if version != QA_TARGET_PROFILE_VERSION:
        return f"the wrapper is QA target profile {version}, expected {QA_TARGET_PROFILE_VERSION}"
    missing = sorted(QA_DOCKER_REQUIRED_VERBS - set(match.group("verbs").split()))
    if missing:
        return f"the wrapper does not answer {', '.join(missing)}"
    return None


def proved_profile_version(playbook_output: str) -> str | None:
    """The profile the role's proof reported in a playbook's output, if any."""
    matches = _PROOF_VERSION.findall(playbook_output)
    return matches[-1] if matches else None


@dataclass(frozen=True)
class QATargetProof:
    """A successful role proof of the current profile, held until it can be recorded.

    Fresh provisioning proves the profile before the key it generated is stored,
    so the proof travels as this typed value to the provisioning-success handler,
    which binds it to the just-persisted connection identity.
    """

    profile_version: str
    proved_at: datetime


def current_profile_proof(playbook_output: str) -> QATargetProof | None:
    """The proof a play reported, if it proved the profile this repository defines."""
    if proved_profile_version(playbook_output) != QA_TARGET_PROFILE_VERSION:
        return None
    return QATargetProof(profile_version=QA_TARGET_PROFILE_VERSION, proved_at=datetime.now(UTC))
