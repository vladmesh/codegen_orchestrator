"""Read models of the admin console: request journeys, the runtime topology, the attention feed.

The console draws generic shapes — steps with facts, placements holding products, attention
items with links — so a runtime change (compose today, Kubernetes later) changes how these are
assembled, not what the frontend renders.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.story import StoryStatus, StoryWaitingOn


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


class JourneyDetail(_ConsoleModel):
    summary: JourneySummary
    request: str | None
    requirements: int | None
    pr_number: int | None
    steps: list[JourneyStep]
    passport: ProductPassport


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
    status: str
    products: list[uuid.UUID]


class TopologyResponse(_ConsoleModel):
    placements: list[PlacementView]
    products: list[ProductView]
    #: Empty until the orchestrator integrates with codegen-platform-services; the console
    #: renders that as a stated absence rather than inventing services.
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


__all__ = [
    "AttentionItem",
    "AttentionResponse",
    "ConsoleKpis",
    "ContainerView",
    "Fact",
    "JourneyAttempt",
    "JourneyDetail",
    "JourneyStage",
    "JourneyStep",
    "JourneySummary",
    "PackageRef",
    "PlacementView",
    "PlatformServiceView",
    "ProductPassport",
    "ProductView",
    "Severity",
    "StepStatus",
    "TopologyResponse",
]
