"""Typed contract for configuration-only bot-audience rollouts.

A conversational audience change (set/add/remove) on a *running* bot is two
effects, not one: the database write, and the redeploy of the already-running
containers so the new `TG_BOT_ALLOWED_TELEGRAM_IDS` reaches them. Both halves
have failed separately in the past — a write whose queue publish was lost left
a QUEUED run nobody would ever publish again — so the rollout's bookkeeping
lives in one place both producers and consumers import.

The record travels in ``Run.run_metadata[BOT_ROLLOUT_METADATA_KEY]`` on the
deploy run the mutation creates. Its state machine is deliberately tiny:

* ``publish_owed`` — the run row is committed, the DeployMessage is not yet
  known to be in ``deploy:queue``. The publisher retries from this state.
* ``published`` — the message was accepted by the stream. At-least-once: a
  lost ACK-style crash here can lead to one republish, which the deploy
  consumer's own guards (project deploy lock, redundant-deploy shortcut)
  absorb.
* ``abandoned`` — bounded publish attempts ran out. Administrators were told;
  nothing more happens automatically and the sweep stops selecting the run.

(The ``superseded`` state is reserved for deploy-lock contention: a cancelled
run's rollout is re-staged by the next mutation rather than chained to a named
successor run today.)

Independently of publishing, ``notify`` tracks whether the owner has been told
the rollout's terminal outcome: ``owed`` until somebody reports it (the PO tool
that observed the verdict itself, or the scheduler sweep delivering the event
for a rollout that finished after the conversation moved on), ``delivered``
afterwards.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

#: ``run_metadata`` key the record lives under on the rollout's deploy run.
BOT_ROLLOUT_METADATA_KEY = "bot_rollout"

#: ``run_metadata`` key recording that the owner still has to hear this
#: rollout's terminal outcome, written when the synchronous conversation window
#: closed before the rollout finished. Flipped to delivered by whoever reports
#: the ending first — the PO tool or the scheduler sweep.
BOT_ROLLOUT_NOTIFY_KEY = "bot_rollout_notify"

#: Prefix of every rollout run id. Distinct from story deploys so an operator
#: can tell one apart in the runs list at a glance.
BOT_ROLLOUT_RUN_ID_PREFIX = "botrollout-"

#: Publish attempts one rollout may spend before a human is called. Counted on
#: the record, so a process that dies mid-retry cannot restart the count.
BOT_ROLLOUT_MAX_PUBLISH_ATTEMPTS = 3


class BotRolloutPublishState(StrEnum):
    """Where the DeployMessage of a staged rollout is."""

    #: The run row exists; the queue write is not yet known to have landed.
    PUBLISH_OWED = "publish_owed"
    #: The stream accepted the message. Delivery to the deploy worker is
    ## at-least-once from here.
    PUBLISHED = "published"
    #: A cancelled run's successor owns the rollout now.
    SUPERSEDED = "superseded"
    #: Attempts ran out; administrators were alerted.
    ABANDONED = "abandoned"


class BotRolloutNotifyState(StrEnum):
    """Whether the owner has heard the rollout's terminal outcome."""

    #: Nobody has reported the ending yet.
    OWED = "owed"
    #: The PO tool saw the verdict itself, or the sweep delivered the event.
    DELIVERED = "delivered"


class BotRolloutNotifyRecord(BaseModel):
    """One owed terminal notification for a rollout still in flight.

    Written by the notify-owed endpoint when the synchronous conversation
    window closed before the rollout finished. The scheduler sweep reads it,
    waits for the run to reach a terminal state, delivers one proactive
    message and flips ``state`` to delivered — so the user hears the ending
    even though the reply that started the rollout has long been sent.
    """

    model_config = ConfigDict(extra="forbid")

    state: BotRolloutNotifyState
    #: The Telegram chat that was promised the outcome. Resolved once by the
    ## endpoint (owner-checked) rather than re-derived by every sweep tick.
    telegram_chat_id: str = ""
    owed_at: datetime
    detail: str | None = None

    @property
    def owed(self) -> bool:
        return self.state is BotRolloutNotifyState.OWED


class BotRolloutRecord(BaseModel):
    """Bookkeeping for one configuration-only rollout, on its deploy run.

    Written before the queue publish (the same order the owner-notification
    seam uses: record first, effect second), so a crash between the two leaves
    work that is visible and resumable rather than a run stranded in QUEUED.
    """

    model_config = ConfigDict(extra="forbid")

    publish: BotRolloutPublishState
    #: How many publish attempts have been spent. Bounded.
    attempts: int = Field(default=0, ge=0)
    #: The application this rollout redeploys, and the exact commit it runs.
    application_id: int
    head_sha: str
    #: When the rollout was staged.
    staged_at: datetime
    #: Why the last attempt did not complete.
    detail: str | None = None

    @property
    def publish_owed(self) -> bool:
        """True while somebody still has to put the message on the queue."""
        return self.publish is BotRolloutPublishState.PUBLISH_OWED


class BotRolloutStatus(StrEnum):
    """What a rollout means for the running service, in PO-facing words."""

    #: The running service carries the new configuration.
    APPLIED = "applied"
    #: Staged but not confirmed — including a cancelled run awaiting its successor.
    PENDING = "pending"
    #: The rollout ran and did not land.
    FAILED = "failed"
    #: Nothing is deployed, so there is nothing to apply to.
    NOT_DEPLOYED = "not_deployed"


def rollout_status_for_run(
    *, run_status: str, result: dict | None, error_message: str | None
) -> tuple[BotRolloutStatus, str]:
    """Map a rollout deploy run onto the PO-facing status, with detail.

    The mapping is the one place that translates run vocabulary into audience
    vocabulary, so every reader — the status endpoint, the PO tool, the sweep —
    says the same thing about the same run. A cancelled run reads as pending:
    cancellation is deploy-lock contention, not an audience verdict, and the
    sweep's publish recovery (or the next mutation's re-staging) decides how
    the rollout actually ends.
    """
    if run_status == "completed":
        return BotRolloutStatus.APPLIED, ""
    if run_status == "failed":
        outcome = (result or {}).get("deploy_outcome")
        detail = error_message or f"deploy outcome: {outcome or 'failed'}"
        return BotRolloutStatus.FAILED, detail
    # queued, running — and cancelled, which means another deploy held the
    # project lock when this rollout was picked up; until the work is retried
    # the honest answer is still "not applied yet".
    return BotRolloutStatus.PENDING, ""
