"""Read models of the admin console: request journeys, the runtime topology, the attention feed.

The console draws generic shapes — steps with facts, placements holding products, attention
items with links — so a runtime change (compose today, Kubernetes later) changes how these are
assembled, not what the frontend renders.
"""

from datetime import datetime
from enum import StrEnum
import re
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.contracts.dto.agent_log import AgentLogKind
from shared.contracts.dto.story import StoryStatus, StoryWaitingOn
from shared.contracts.dto.task import TaskStatus, TaskType


class _ConsoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(_ConsoleModel):
    """One label/value pair the console shows as is, without interpreting it."""

    label: str
    value: str


class JourneyStage(StrEnum):
    BRIEF = "brief"
    PLAN = "plan"
    INSTALL = "install"
    BUILD = "build"
    REVIEW = "review"
    DEPLOY = "deploy"
    VERIFY = "verify"
    LIVE = "live"


class StepStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class JourneyAttempt(_ConsoleModel):
    """One run (or catalog install) inside a step."""

    id: str
    kind: Literal["engineering", "deploy", "qa", "install"]
    status: str
    task_id: str | None
    actor: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class JourneyStep(_ConsoleModel):
    stage: JourneyStage
    status: StepStatus
    started_at: datetime | None
    finished_at: datetime | None
    attempts: list[JourneyAttempt]
    facts: list[Fact]


class JourneySummary(_ConsoleModel):
    story_id: str
    title: str
    project_id: uuid.UUID
    project_title: str
    status: StoryStatus
    waiting_on: StoryWaitingOn
    current_stage: JourneyStage | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class PackageRef(_ConsoleModel):
    name: str
    version: str
    kind: Literal["package", "library"]


class ContainerView(_ConsoleModel):
    """A deployed service of a product (an Application row today, a Deployment later)."""

    name: str
    status: str
    placement: str
    port: int | None
    reserved_ram_mb: int
    response_time_ms: int | None
    uptime_pct_24h: float | None
    deployed_sha: str | None


class ProductPassport(_ConsoleModel):
    """What a product is built from, what it is connected to and where it runs."""

    modules: list[str]
    packages: list[PackageRef]
    containers: list[ContainerView]
    user_secrets: list[str]


class StoryLinkKind(StrEnum):
    """Why a pull request outside the story's own runs is linked to it."""

    #: A reusable module the platform proposed to the module library after the product went live.
    MODULE_PROPOSAL = "module_proposal"


#: A linked pull request lives on GitHub: `https://github.com/<owner>/<repo>/pull/<number>`.
GITHUB_PULL_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*$")


class StoryLinkCreate(_ConsoleModel):
    kind: StoryLinkKind
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def _github_pull_request(cls, url: str) -> str:
        if not GITHUB_PULL_URL.match(url):
            raise ValueError(
                "url must be a GitHub pull request: https://github.com/<owner>/<repo>/pull/<n>"
            )
        return url


class StoryLinkView(_ConsoleModel):
    id: int
    kind: StoryLinkKind
    title: str
    url: str
    created_at: datetime


class TaskBoardColumn(StrEnum):
    BACKLOG = "backlog"
    IN_DEVELOPMENT = "in_development"
    CI = "ci"
    DONE = "done"


class TaskOrigin(StrEnum):
    """Why a task exists: planned with the story, or a fix after a failed CI or QA run."""

    PLANNED = "planned"
    CI_FIX = "ci_fix"
    QA_FIX = "qa_fix"


class TaskBoardAttempt(_ConsoleModel):
    """One engineering run of a task; ``number`` counts from 1 in start order."""

    number: int = Field(ge=1)
    run_id: str
    status: str
    worker_id: str | None
    actor: str | None
    started_at: datetime | None
    finished_at: datetime | None


class TaskBoardEntry(_ConsoleModel):
    task_id: str
    title: str
    type: TaskType
    status: TaskStatus
    column: TaskBoardColumn
    origin: TaskOrigin
    #: Oldest first.
    attempts: list[TaskBoardAttempt]
    #: The worker of the latest attempt; null with no attempt or none recorded on it yet.
    worker_id: str | None
    attempt_count: int = Field(ge=0)


class JourneyDetail(_ConsoleModel):
    summary: JourneySummary
    request: str | None
    requirements: int | None
    pr_number: int | None
    steps: list[JourneyStep]
    passport: ProductPassport
    #: Pull requests an operator or automation linked to the story, oldest first.
    links: list[StoryLinkView]
    #: Every task of the story, oldest first.
    task_board: list[TaskBoardEntry]


class PlacementView(_ConsoleModel):
    handle: str
    role: Literal["control", "product"]
    status: str
    public_ip: str
    capacity_cpu: int
    capacity_ram_mb: int
    used_ram_mb: int
    cpu_usage_pct: float | None
    last_health_check: datetime | None


class ProductView(_ConsoleModel):
    project_id: uuid.UUID
    title: str
    slug: str
    status: str
    latest_story_id: str | None
    passport: ProductPassport


class PlatformServiceView(_ConsoleModel):
    name: str
    #: `up`, `down` or `unreachable` from a probe of the declared base URL; `undeclared` for a
    #: service a deployed product uses that `PLATFORM_SERVICES` does not name.
    status: str
    products: list[uuid.UUID]


class TopologyResponse(_ConsoleModel):
    placements: list[PlacementView]
    products: list[ProductView]
    #: Services declared in `PLATFORM_SERVICES` plus any a product's latest successful deploy
    #: drew a platform key or endpoint from (`deployment_info.platform_services`).
    platform_services: list[PlatformServiceView]


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class AttentionItem(_ConsoleModel):
    kind: Literal["task", "story", "incident", "application", "queue"]
    severity: Severity
    title: str
    detail: str | None
    since: datetime | None
    project_id: uuid.UUID | None
    project_title: str | None
    story_id: str | None
    task_id: str | None
    application_id: int | None
    server_handle: str | None


class ConsoleKpis(_ConsoleModel):
    active_journeys: int = Field(ge=0)
    running_runs: int = Field(ge=0)
    queued_runs: int = Field(ge=0)
    live_products: int = Field(ge=0)
    degraded_containers: int = Field(ge=0)
    median_lead_time_minutes_7d: float | None


class AttentionResponse(_ConsoleModel):
    kpis: ConsoleKpis
    items: list[AttentionItem]


class LiveLogSource(StrEnum):
    """Which agent wrote a live log line, from the id set its ``attempt_id`` belongs to."""

    ARCHITECT = "architect"
    DEVELOPER = "developer"
    QA = "qa"
    #: A worker's line whose ``attempt_id`` matches no Run.
    WORKER = "worker"


class LiveLogUnavailable(StrEnum):
    """Why a live log read returned no lines from the log store."""

    NOT_CONFIGURED = "log_store_not_configured"
    QUERY_FAILED = "log_store_query_failed"
    TIMED_OUT = "log_store_timed_out"


class LiveLogLine(_ConsoleModel):
    at: datetime
    source: LiveLogSource
    attempt_id: str
    seq: int = Field(ge=0)
    kind: AgentLogKind
    text: str


class LiveLogResponse(_ConsoleModel):
    #: Oldest to newest; the lines of one attempt in ``seq`` order.
    lines: list[LiveLogLine]
    #: Null when Loki answered; otherwise ``lines`` is empty.
    unavailable: LiveLogUnavailable | None
    #: How many of the story's oldest attempt ids fell beyond the query cap and were not read.
    omitted_attempts: int = Field(ge=0)


class RepositoryRef(_ConsoleModel):
    owner: str
    name: str


class RepositoryHeadSource(StrEnum):
    """Where the story's head commit was read from."""

    PULL_REQUEST = "pull_request"
    BRANCH = "branch"


class RepositoryHead(_ConsoleModel):
    source: RepositoryHeadSource
    ref: str
    sha: str


class ChangedFileStatus(StrEnum):
    """GitHub's status of a file in a comparison."""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class ChangedFile(_ConsoleModel):
    path: str
    status: ChangedFileStatus
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    #: Null when GitHub renders no patch: a binary file or a diff too large to show.
    patch: str | None
    #: The patch was cut at the read's patch length bound.
    truncated: bool


class RepositoryUnavailableReason(StrEnum):
    """Why a story's repository read returned no head, files or changes."""

    NO_REPOSITORY = "no_repository"
    NO_STORY_BRANCH = "no_story_branch"
    GITHUB_UNAVAILABLE = "github_unavailable"


class RepositoryUnavailable(_ConsoleModel):
    reason: RepositoryUnavailableReason
    #: For ``github_unavailable``: GitHub's status code, or the exception class of the failure.
    detail: str | None


class JourneyRepository(_ConsoleModel):
    """The story's product repository at its head and the diff of the story's own changes."""

    #: Null only for ``no_repository``.
    repository: RepositoryRef | None
    head: RepositoryHead | None
    #: Every file path of the tree at ``head.sha``, sorted.
    files: list[str]
    changes: list[ChangedFile]
    #: How many changed files fell beyond the read's file bound and were not returned.
    omitted_changes: int = Field(ge=0)
    #: False when ``omitted_changes`` is a lower bound: a branch comparison at GitHub's file cap.
    omitted_changes_exact: bool
    #: Null when GitHub answered; otherwise ``head`` is null and ``files`` and ``changes`` empty.
    unavailable: RepositoryUnavailable | None


__all__ = [
    "AttentionItem",
    "ChangedFile",
    "ChangedFileStatus",
    "AttentionResponse",
    "ConsoleKpis",
    "ContainerView",
    "Fact",
    "JourneyAttempt",
    "JourneyDetail",
    "JourneyRepository",
    "JourneyStage",
    "JourneyStep",
    "JourneySummary",
    "LiveLogLine",
    "LiveLogResponse",
    "LiveLogSource",
    "LiveLogUnavailable",
    "PackageRef",
    "PlacementView",
    "PlatformServiceView",
    "ProductPassport",
    "ProductView",
    "RepositoryHead",
    "RepositoryHeadSource",
    "RepositoryRef",
    "RepositoryUnavailable",
    "RepositoryUnavailableReason",
    "Severity",
    "StoryLinkCreate",
    "StoryLinkKind",
    "StoryLinkView",
    "StepStatus",
    "TaskBoardAttempt",
    "TaskBoardColumn",
    "TaskBoardEntry",
    "TaskOrigin",
    "TopologyResponse",
]
