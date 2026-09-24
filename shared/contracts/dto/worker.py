"""Worker DTOs and enums — single source of truth for worker statuses."""

from enum import StrEnum


class WorkerStatus(StrEnum):
    BUILDING = "BUILDING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DEAD = "DEAD"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    GONE = "GONE"
    UNKNOWN = "UNKNOWN"


#: Statuses in which a worker container is no longer executing anything. A
#: worker in one of these is past being late — there is nothing left to wait
#: for, so an attempt behind it is terminal at once rather than after a silence
#: window meant for a worker that might still speak.
WORKER_TERMINAL_STATUSES: frozenset[WorkerStatus] = frozenset(
    {
        WorkerStatus.DEAD,
        WorkerStatus.FAILED,
        WorkerStatus.STOPPED,
        WorkerStatus.GONE,
    }
)


#: How long a creation failure stays readable after its worker is torn down.
WORKER_CREATION_FAILURE_TTL_SECONDS = 600


def worker_creation_failure_key(worker_id: str) -> str:
    """Redis hash holding why a worker's creation failed.

    `worker:status` and `worker:error` are deleted with the worker, and a failed
    creation queues that deletion at once, so a spawner polling for readiness
    can find both gone before it ever read them. This key is not part of the
    worker's teardown; it expires on its own.
    """
    return f"worker:creation-failure:{worker_id}"
