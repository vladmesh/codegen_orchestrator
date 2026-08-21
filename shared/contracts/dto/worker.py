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
