"""Why a worker creation failed, in words that survive an empty exception.

A creation command is ACKed before the heavy work, so the only account of a
failure after that point is what this side writes down. `str(exc)` is not that
account: a bare `TimeoutError` — the shape of a checkout that ran out its
bound — stringifies to the empty string, and the log line it produced read
`error=''`, which is indistinguishable from no failure at all.
"""

from __future__ import annotations

WORKER_CREATION_STEP_ATTR = "worker_creation_step"


def mark_worker_creation_step(exc: BaseException, step: str) -> BaseException:
    """Stamp the step a creation was in, unless an inner step already named one.

    Returns the same exception, so a handler can stamp and describe in one line.
    """
    try:
        if not getattr(exc, WORKER_CREATION_STEP_ATTR, None):
            setattr(exc, WORKER_CREATION_STEP_ATTR, step)
    except (AttributeError, TypeError):  # an exception that refuses attributes
        pass
    return exc


def worker_creation_step(exc: BaseException) -> str | None:
    """The creation step stamped on this exception, if it carries one."""
    step = getattr(exc, WORKER_CREATION_STEP_ATTR, None)
    return step if isinstance(step, str) and step else None


def worker_creation_failure_reason(exc: BaseException) -> str:
    """A non-empty reason for one creation failure: type, message, and step.

    The type is always known and is never empty, so the reason never is either.
    """
    message = str(exc).strip()
    described = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    step = worker_creation_step(exc)
    return f"{described} (step: {step})" if step else described
