"""What still has to happen for a QA run to start, written down before it does.

The handoff from a successful deploy to QA is several writes: the story moves to
TESTING, a QA run appears, a temporary access grant is recorded, a message is
published. A process that dies part-way through used to leave a story nobody
supervises any more, a queued run nobody will ever start, and no grant for the
sweep to find.

So the plan is decided once and stored on the QA run itself, in the same write
that creates it. From then on the run carries everything needed to finish the
handoff, and any later tick can do it — the decision does not live in the process
that made it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from shared.contracts.git_ref import CommitSha
from shared.contracts.queues.qa import QAMessage

# run_metadata keys the handoff is recovered from and stamped into.
QA_HANDOFF_KEY = "qa_handoff"
QA_DISPATCHED_AT_KEY = "qa_dispatched_at"


class TemporaryAccessRequest(BaseModel):
    """The access this QA run has to borrow before it can reach the bot."""

    model_config = ConfigDict(extra="forbid")

    target_application_id: int
    target_base_url: str
    head_sha: CommitSha


class QAHandoffPlan(BaseModel):
    """Everything the handoff needs, readable from the QA run after a restart."""

    model_config = ConfigDict(extra="forbid")

    qa_message: QAMessage
    # None when the deployed bot already admits the QA identity, so the message
    # goes straight to the queue and no access is handed out.
    access: TemporaryAccessRequest | None = None
