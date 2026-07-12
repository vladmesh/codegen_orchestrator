"""Canonical cross-service vocabularies.

Single source of truth for the enums that producers and consumers must agree on.
Anything comparing an agent, action, result status, or lifecycle event imports
from here instead of restating a `Literal[...]` or a local enum.
"""

from enum import StrEnum
from typing import Literal


class AgentType(StrEnum):
    """Coding agent that runs inside a developer worker."""

    CLAUDE = "claude"  # Claude Code
    FACTORY = "factory"  # Factory.ai Droid
    NOOP = "noop"  # No-op runner for E2E testing (empty commit + push)


class WorkerCliKind(StrEnum):
    """CLI-agent wire identity reported on `worker:events`.

    Deliberately distinct from :class:`AgentType`: these are the historical
    `worker_type` values a running CLI reports about itself, and they do not map
    one-to-one onto the agent we ask for (claude/factory/noop). Kept separate on
    purpose, not merged.
    """

    DROID = "droid"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class ActionType(StrEnum):
    """Kind of code change an engineering task carries out.

    Deploy work reuses these values but has its own superset
    (:class:`shared.contracts.queues.deploy.DeployAction`) that adds the
    deploy-only `stop`/`undeploy` operations. Planning tasks use
    :class:`shared.contracts.dto.task.TaskType`, which adds `refactor`.
    """

    CREATE = "create"
    FEATURE = "feature"
    FIX = "fix"


class ResultStatus(StrEnum):
    """Terminal status of an async operation result."""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


class LifecycleEvent(StrEnum):
    """Progress/lifecycle event kind emitted while long-running work runs.

    The enum is the canonical *member set*. Individual wires accept only the
    slice their producers actually emit — see the field-specific `Literal`
    subsets below. The slices deliberately differ (progress streams never emit
    `stopped`; the worker-lifecycle stream never emits `progress`), so they are
    kept explicit rather than collapsed into one merged set.
    """

    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


# Progress-style streams: task start → incremental progress → terminal outcome.
# Used by ProgressEvent.type and WorkerEvent.event_type. No `stopped`.
TaskProgressKind = Literal[
    LifecycleEvent.STARTED,
    LifecycleEvent.PROGRESS,
    LifecycleEvent.COMPLETED,
    LifecycleEvent.FAILED,
]

# Worker container lifecycle: start → terminal outcome → explicit stop.
# Used by WorkerLifecycleEvent.event. No `progress`.
WorkerLifecycleKind = Literal[
    LifecycleEvent.STARTED,
    LifecycleEvent.COMPLETED,
    LifecycleEvent.FAILED,
    LifecycleEvent.STOPPED,
]
