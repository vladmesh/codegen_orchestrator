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
from .project_spec import (
    EntryPointSpec,
    InfrastructureSpec,
    ProjectInfo,
    ProjectSpecYAML,
)
from .time4vps import (
    Time4VPSOSTemplate,
    Time4VPSServer,
    Time4VPSServerDetails,
    Time4VPSTask,
)
from .tool_groups import (
    TOOL_DOCS,
    ToolGroup,
    get_instructions_content,
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
    # Project Spec
    "ProjectSpecYAML",
    "ProjectInfo",
    "EntryPointSpec",
    "InfrastructureSpec",
    # Worker events
    "WorkerEvent",
    "WorkerStarted",
    "WorkerProgress",
    "WorkerCompleted",
    "WorkerFailed",
    "parse_worker_event",
    # Tool Groups
    "ToolGroup",
    "TOOL_DOCS",
    "get_instructions_content",
    # Orchestrator State is local to LangGraph
    # Tools Results are local to LangGraph
]
