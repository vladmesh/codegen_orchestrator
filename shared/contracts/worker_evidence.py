"""What a dynamic worker was, written down the moment before it vanishes.

Ownership labels make a *dead* worker attributable: a container that exited
keeps its labels, so `docker ps -a --filter label=com.codegen.run.id=<run>`
still finds it. They cannot make a *removed* worker attributable — Docker
forgets a removed container completely, and `delete_worker` removes the
container before it deletes `worker:meta:<id>`. Between those two moments the
worker's whole ending is readable, and one moment later nothing of it exists.

So the component that removes a worker writes the ending down first. This module
is the shape of that record and the key it lives under: run-scoped, keyed by the
ownership the worker already carries, and deliberately *not* deleted with
`worker:meta`. A run's evidence collector then has two sources and no race to
win — the containers still present, found by label, and this record for the ones
already removed.

Precedence, because observability must never own cleanup: capture is bounded and
every failure is recorded rather than raised. A worker whose ending could not be
read is still removed, and its record says which fact was lost and why. That is
the point of `RemovalFact` — no field here is ever a bare empty value, because
"the agent printed nothing" and "the log could not be read in time" are
different findings and an artifact that cannot tell them apart is worthless.

Cleanup is never wedged by observability, but the two destructive steps are not
equal: removing the container frees resources and always proceeds, while
deleting `worker:meta:<id>` destroys the worker's last durable name. So the
metadata is deleted only once this record exists. When the record cannot be
stored, `delete_worker` keeps `worker:meta` instead — a leaked key a label sweep
collects later, rather than a worker no source can name.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from shared.contracts.queues.worker import WorkerOwnership

__all__ = [
    "REMOVAL_LOG_TAIL_LINES",
    "REMOVAL_LOG_TAIL_MAX_CHARS",
    "SECRET_ENV_NAME",
    "RemovalFact",
    "RemovedWorkerEvidence",
    "removed_worker_evidence_key",
    "secret_env_values",
]

# What "bounded" means for the log tail this record carries, for the writer and
# every reader alike. Lines bound what Docker is asked for; characters bound
# what a pathological single line can do to the record.
REMOVAL_LOG_TAIL_LINES = 200
REMOVAL_LOG_TAIL_MAX_CHARS = 12_000

# The redaction rule, stated once: any environment value whose *name* looks like
# a credential is replaced wherever it appears in the tail. Same rule
# worker-wrapper redacts its transcripts with
# (packages/worker-wrapper/src/worker_wrapper/observability.py).
SECRET_ENV_NAME = re.compile(r"(?:key|secret|token|password|credential|authorization)", re.I)

# One hash per run, field per worker. Run-scoped so a run reads its own workers
# and nobody else's, and separate from `worker:meta:<id>` so that deleting the
# worker's metadata — which `delete_worker` does immediately after removing the
# container — cannot take the evidence with it.
_REMOVED_WORKER_EVIDENCE_KEY = "worker:evidence:removed:{run_id}"


def removed_worker_evidence_key(run_id: str) -> str:
    """The Redis hash holding one run's removed-worker records."""
    if not run_id:
        raise ValueError("removed-worker evidence is run-scoped; there is no unowned evidence")
    return _REMOVED_WORKER_EVIDENCE_KEY.format(run_id=run_id)


def secret_env_values(environment: dict[str, str]) -> list[str]:
    """The values a container's environment says are secret, by their names."""
    return [value for name, value in environment.items() if value and SECRET_ENV_NAME.search(name)]


class RemovalFact(BaseModel):
    """One fact read at removal time, or the stated reason it could not be read.

    Exactly one of the two is set. A fact with neither is an empty field that
    reads like "nothing happened"; a fact with both is a capture that cannot
    decide whether it succeeded.
    """

    value: Any = None
    missed_reason: str | None = None

    @model_validator(mode="after")
    def _a_fact_is_either_a_value_or_a_reason(self) -> RemovalFact:
        if (self.value is None) == (self.missed_reason is None):
            raise ValueError("a removal fact carries either a value or the reason it is missing")
        return self

    @classmethod
    def read(cls, value: Any) -> RemovalFact:
        """A fact that was read. `None` is not a value — use `missed`."""
        if value is None:
            raise ValueError("a read fact has a value; an absent one is a missed fact")
        return cls(value=value)

    @classmethod
    def missed(cls, reason: str) -> RemovalFact:
        if not reason:
            raise ValueError("a missed fact must say why it was missed")
        return cls(missed_reason=reason)

    @property
    def was_read(self) -> bool:
        return self.missed_reason is None


class RemovedWorkerEvidence(BaseModel):
    """One worker's ending, captured by whoever removed its container.

    The ownership is the worker's own, taken from the record written at its
    creation, so this evidence is attributed to the same run, project and
    attempt as the container's labels were — not to whatever asked for the
    deletion.
    """

    worker_id: str = Field(min_length=1)
    container: str = Field(min_length=1)
    ownership: WorkerOwnership
    # ISO-8601 UTC, stamped by the remover: the last instant this worker existed.
    removed_at: str = Field(min_length=1)
    # The `DeleteWorkerCommand` reason, when the deletion carried one.
    delete_reason: str | None = None
    worker_type: RemovalFact
    agent_type: RemovalFact
    image: RemovalFact
    state: RemovalFact
    exit_code: RemovalFact
    log_tail: RemovalFact
    # The host directory worker-wrapper retained this worker's transcript in.
    # It outlives the container, so a Codex exit stays attributable afterwards —
    # but only if something wrote down where to look before the mount was gone.
    transcript_dir: RemovalFact
