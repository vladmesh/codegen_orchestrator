"""Reading back what a deployed service actually holds in its environment.

A deploy is a request, not an effect. Between asking for a value to change and
the running service having it stands GitHub Actions, which this system does not
own and cannot order about. A deploy that reported success is therefore evidence
that the request was accepted, not that the effect is in place.

So a caller that has to know a value is gone asks for the running service's own
environment to be read. The read runs where the SSH key and the playbooks already
are (``infra-service``), and answers one of two things: what the service has, or
that the reading could not be done. The second is not a failure of the caller's
subject — it is the absence of an answer, and callers must treat it that way.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.base import BaseMessage


class EnvObservationRequest(BaseMessage):
    """Read one environment slot of one deployed service."""

    project_id: str = Field(min_length=1)
    # Where the service runs and under which directory the deploy put it.
    server_handle: str = Field(min_length=1)
    service_slug: str = Field(min_length=1)
    env_key: str = Field(min_length=1)


class EnvObservationOutcome(StrEnum):
    """Whether the running service could be read at all."""

    OBSERVED = "observed"
    # SSH down, playbook failed, nothing running to read. Neither a success nor
    # a failure of whatever the caller wanted to confirm.
    UNREACHABLE = "unreachable"


class EnvObservationResult(BaseModel):
    """What the running service has, or why it could not be asked."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    outcome: EnvObservationOutcome
    env_key: str = Field(min_length=1)
    # True when a running container of the service carries a non-empty value in
    # the slot. None when nothing was read; reading it as "absent" would turn an
    # unanswered question into a confirmation.
    present: bool | None = None
    containers: int = 0
    detail: str = ""

    @model_validator(mode="after")
    def _an_answer_is_an_answer_and_a_silence_is_not(self) -> EnvObservationResult:
        if self.outcome is EnvObservationOutcome.OBSERVED and self.present is None:
            raise ValueError("an observed environment must say whether the slot is filled")
        if self.outcome is EnvObservationOutcome.UNREACHABLE:
            if self.present is not None:
                raise ValueError("an unreachable service cannot report what it holds")
            if not self.detail:
                raise ValueError("an unreachable service must say what stopped the reading")
        return self


def env_observation_result_key(request_id: str) -> str:
    """Where the reader leaves its answer for the caller to pick up.

    The caller and the reader are different services on different ticks, so the
    answer outlives the request rather than being returned to a waiting call.
    """
    return f"env-observation:result:{request_id}"


def env_observation_pending_key(request_id: str) -> str:
    """Marks a request that has been asked and not yet answered.

    Set with an expiry by the caller before it publishes, so a sweep that runs
    every tick asks once per window instead of queueing a playbook run per tick.
    Losing it costs a repeated read, which is harmless: reading is not a change.
    """
    return f"env-observation:pending:{request_id}"


# How long the answer stays readable. Long enough for a caller that was restarted
# to still find it, short enough that a stale answer cannot settle a later
# question — request ids are per attempt, so a stale one is never re-read anyway.
ENV_OBSERVATION_RESULT_TTL_SECONDS = 3600
