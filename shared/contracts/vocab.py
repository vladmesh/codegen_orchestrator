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
    CODEX = "codex"  # OpenAI Codex CLI
    NOOP = "noop"  # No-op runner for E2E testing (empty commit + push)


# Who may perform exploratory QA. The owner-set contract names exactly two
# executors, and both are subscription CLIs whose session lives on the
# management host: Claude Code by default, Codex when assigned explicitly.
# `factory` is excluded because it runs on a provider API key, and `noop`
# because it performs no testing at all — a QA run started on either is not a
# QA run, so both are refused where they are configured rather than allowed to
# become a container. Developer workers keep the full `AgentType`.
QA_EXECUTOR_AGENT_TYPES: frozenset[AgentType] = frozenset({AgentType.CLAUDE, AgentType.CODEX})

# The same restriction as a type, for fields that declare it rather than check it.
QAExecutorAgentType = Literal[AgentType.CLAUDE, AgentType.CODEX]


class WorkerType(StrEnum):
    """What a worker container exists to do.

    `WorkerConfig.worker_type` states the same two values as a `Literal` because
    that is the wire; this enum is what code compares against and what the
    control-plane allowlist is keyed by, so the spelling lives in one place.
    `shared/tests/unit/test_vocab.py` fails if the two drift apart.
    """

    DEVELOPER = "developer"  # writes code in a pre-scaffolded repository workspace
    QA = "qa"  # the central exploratory-QA executor: no repository, nothing to commit


class WorkerCliKind(StrEnum):
    """CLI-agent wire identity reported on `worker:events`.

    Deliberately distinct from :class:`AgentType`: these are the historical
    `worker_type` values a running CLI reports about itself. The Codex spelling
    overlaps with :class:`AgentType`, while the Claude and Factory spellings do
    not. The concepts stay separate because this field reports CLI identity,
    not the requested developer-worker type.
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
    # A stale operation whose completion was superseded by a newer one for the
    # same target. It is a no-op outcome: consumers must not treat it as a
    # failure (no status mutation, no failure notification).
    SUPERSEDED = "superseded"


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
