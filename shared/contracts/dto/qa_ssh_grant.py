"""The record that a QA run was given SSH reach to a target, written before it was.

A QA run reaches its target with a one-shot key installed in the target's
`authorized_keys`. Removing it in the runner's `finally` is not enough on its
own: the append can land while the answer is lost, and then the only process
that knew a key might be out there is the one that just failed. Nothing would
look for it.

So the fact of the grant is not inferred from a successful install. It is
written down before the install is attempted, on the QA run itself — the same
place the deploy already leaves its `qa_handoff` plan — and the record is what
owns the key from then on. `ISSUING` means "a key may be installed"; only a
readback proving the key is gone moves it to `RELEASED`. Anything else stays for
the sweep, which keeps revoking and, while it cannot, keeps the run's outcome
saying so.

This is deliberately not a second copy of `temporary_access`: that grant hands a
Telegram identity to a deployed bot and is settled by deploys, which is a
different lifecycle with a different subject. What is borrowed from it is its
shape — a durable record, a sweep that reconciles from state rather than from
the happy path, and a failure that lands on the run instead of in a log line.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# run_metadata key the record lives under, alongside `qa_handoff`.
QA_SSH_GRANT_KEY = "qa_ssh_grant"


class QASshGrantState(StrEnum):
    """What is known about the key this run may hold on its target."""

    # Written before the install is attempted. A key may or may not be on the
    # target; the sweep must assume it is.
    ISSUING = "issuing"
    # The install returned success, so a key is on the target.
    OPEN = "open"
    # The target was read back and the key is not there.
    RELEASED = "released"


class QASshGrant(BaseModel):
    """One run's SSH reach into one target, and what is known about removing it."""

    model_config = ConfigDict(extra="forbid")

    # Identifies exactly this run's authorized_keys line, so removal never
    # touches a key belonging to anything else.
    marker: str
    server_handle: str
    server_ip: str
    ssh_user: str
    state: QASshGrantState
    issued_at: datetime
    revoke_attempts: int = Field(default=0, ge=0)
    # Why the last removal attempt did not prove the key gone.
    detail: str | None = None

    @property
    def held(self) -> bool:
        """True while the target may still admit this run's key."""
        return self.state is not QASshGrantState.RELEASED
