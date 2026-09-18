"""Pydantic models for the worker HTTP result server.

`ResultRequest` is the external contract for the agent's POST /result call.
`to_worker_result` converts a validated request into the typed
:data:`WorkerResult` contract published on the ``worker:{id}:output`` stream.

Naming the failed step
----------------------

The scripted developer runner
(``worker_wrapper.runners.noop``) names every step it runs and POSTs the failed
one as ``step``/``error_class``/``exit_code`` beside ``reason``. Those three
fields are declared here rather than read back out of ``reason`` by a caller:
the runner already sends them, so the alternative was to let Pydantic drop them
at this boundary and then parse the step name out of a prose sentence further
down the pipeline — one producer, one consumer, and a string format nobody owns
in between.

They are folded into the single ``block_reason`` the typed worker-result
contract carries, because widening that contract is a change to
``shared/contracts/`` and this card has no mandate for one. ``block_reason`` is
prose for a human, and a failed run now says which step failed in it.

``stderr`` is deliberately *not* declared: the runner prints the same
credential-redacted tail to stdout, which the wrapper already retains as
``agent_stdout_tail``. Declaring it here would put a second copy of it into
every blocked reason.
"""

from pydantic import BaseModel, field_validator, model_validator

from shared.contracts.queues.worker_result import (
    WorkerBlockedResult,
    WorkerCompletedResult,
)


class ResultRequest(BaseModel):
    """POST /result — unified worker result (success or failure).

    success=true requires commit + summary.
    success=false requires reason, and may name the step that failed.
    """

    success: bool
    commit: str | None = None
    summary: str | None = None
    reason: str | None = None
    #: The runner's name for the step that failed (``setup``, ``commit``, …).
    step: str | None = None
    #: The runner's classification of the failure (``SetupFailed``, …).
    error_class: str | None = None
    #: The exit code the failed step returned.
    exit_code: int | None = None

    @field_validator("commit", "summary", "reason", "step", "error_class", mode="before")
    @classmethod
    def strip_strings(cls, v: str | None) -> str | None:
        if isinstance(v, str) and not v.strip():
            raise ValueError("must not be empty")
        return v

    @model_validator(mode="after")
    def check_required_fields(self) -> "ResultRequest":
        if self.success:
            if not self.commit:
                raise ValueError("commit is required when success=true")
            if not self.summary:
                raise ValueError("summary is required when success=true")
            if self.step or self.error_class or self.exit_code is not None:
                raise ValueError("step, error_class and exit_code describe a failure")
        elif not self.reason:
            raise ValueError("reason is required when success=false")
        return self


def failure_reason(request: ResultRequest) -> str:
    """The blocked reason, with whatever the runner said about the failed step.

    The step is appended rather than substituted: ``reason`` is what the agent
    chose to say, and a reader of a blocked result keeps reading the same
    sentence it always did, now followed by the facts behind it.
    """
    named = [
        f"{name}={value}"
        for name, value in (
            ("step", request.step),
            ("error_class", request.error_class),
            ("exit_code", request.exit_code),
        )
        if value is not None
    ]
    if not named:
        return request.reason or ""
    return f"{request.reason} ({', '.join(named)})"


def to_worker_result(
    request: ResultRequest,
) -> WorkerCompletedResult | WorkerBlockedResult:
    """Build the typed worker result from a validated HTTP request.

    - success=true  → completed (commit_sha + content)
    - success=false → blocked (block_reason, naming the failed step)
    """
    if request.success:
        return WorkerCompletedResult(commit_sha=request.commit, content=request.summary)
    return WorkerBlockedResult(block_reason=failure_reason(request))
