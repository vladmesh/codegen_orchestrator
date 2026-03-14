"""Application DTO — runtime state of a deployable unit on a server."""

from enum import StrEnum


class ApplicationStatus(StrEnum):
    """Runtime state of an application on a server."""

    NOT_DEPLOYED = "not_deployed"
    RUNNING = "running"
    STOPPED = "stopped"
    DOWN = "down"
    DEGRADED = "degraded"
