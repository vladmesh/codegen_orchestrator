"""Shared Pydantic schemas for codegen_orchestrator services.

This module provides typed data structures for:
- External API responses (Time4VPS, GitHub)
- Orchestrator state components
- Tool return values

Usage:
    from shared.schemas import RepoInfo, AllocatedResource, Time4VPSServer
"""

from .github import (
    GitHubFileContent,
    GitHubInstallation,
    GitHubRepository,
)
from .time4vps import (
    Time4VPSOSTemplate,
    Time4VPSServer,
    Time4VPSServerDetails,
    Time4VPSTask,
)
from .worker_events import (
    WorkerCompleted,
    WorkerEvent,
    WorkerFailed,
    WorkerProgress,
    WorkerStarted,
    parse_worker_event,
)

__all__ = [
    # Time4VPS
    "Time4VPSServer",
    "Time4VPSServerDetails",
    "Time4VPSTask",
    "Time4VPSOSTemplate",
    # GitHub
    "GitHubInstallation",
    "GitHubRepository",
    "GitHubFileContent",
    # Worker events
    "WorkerEvent",
    "WorkerStarted",
    "WorkerProgress",
    "WorkerCompleted",
    "WorkerFailed",
    "parse_worker_event",
    # Orchestrator State is local to LangGraph
    # Tools Results are local to LangGraph
]
