"""Worker DTOs and enums — single source of truth for worker statuses."""

from enum import StrEnum


class WorkerStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DEAD = "DEAD"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    GONE = "GONE"
    UNKNOWN = "UNKNOWN"
