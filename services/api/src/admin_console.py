"""Assemble the admin console read models from the rows the pipeline already writes.

Pure builders take loaded rows and return console DTOs; the `load_*` functions are the only
code that queries. Nothing here writes, and nothing invents a value a row does not carry: a
stage without evidence is `pending` or `skipped`, never guessed done.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.deployment import DeploymentResult
from shared.contracts.dto.executor_decision import ExecutorDecision
from shared.contracts.dto.incident import IncidentStatus, IncidentType
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus, StoryWaitingOn
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.models import (
    Application,
    Deployment,
    Incident,
    PortAllocation,
    ProductBrief,
    Project,
    Repository,
    Run,
    Server,
    Story,
    Task,
)

from .queue_snapshot import get_queue_snapshot
from .schemas.admin_console import (
    AttentionItem,
    AttentionResponse,
    ConsoleKpis,
    ContainerView,
    Fact,
    JourneyAttempt,
    JourneyDetail,
    JourneyStage,
    JourneyStep,
    JourneySummary,
    PackageRef,
    PlacementView,
    ProductPassport,
    ProductView,
    Severity,
    StepStatus,
    TopologyResponse,
)

CONTROL_HOST_ROLE = "control_host"
JOURNEY_LIST_LIMIT = 50
ATTENTION_ROW_LIMIT = 50
ERROR_PREVIEW_CHARS = 500

STAGES: tuple[JourneyStage, ...] = tuple(JourneyStage)

_STATUS_STAGE: dict[StoryStatus, JourneyStage] = {
    StoryStatus.CREATED: JourneyStage.PLAN,
    StoryStatus.REOPENED: JourneyStage.PLAN,
    StoryStatus.IN_PROGRESS: JourneyStage.BUILD,
    StoryStatus.PR_REVIEW: JourneyStage.REVIEW,
    StoryStatus.DEPLOYING: JourneyStage.DEPLOY,
    StoryStatus.TESTING: JourneyStage.VERIFY,
    StoryStatus.WAITING_HUMAN_REVIEW: JourneyStage.BUILD,
    StoryStatus.WAITING_USER_SECRET: JourneyStage.DEPLOY,
    StoryStatus.COMPLETED: JourneyStage.LIVE,
}

_WAITING_STAGE: dict[StoryWaitingOn, JourneyStage] = {
    StoryWaitingOn.CI: JourneyStage.REVIEW,
    StoryWaitingOn.DEPLOY: JourneyStage.DEPLOY,
    StoryWaitingOn.QA: JourneyStage.VERIFY,
    StoryWaitingOn.USER_SECRET: JourneyStage.DEPLOY,
    StoryWaitingOn.HUMAN_REVIEW: JourneyStage.BUILD,
    StoryWaitingOn.RESOURCES: JourneyStage.BUILD,
}

_TERMINAL_STORY = {StoryStatus.COMPLETED, StoryStatus.FAILED, StoryStatus.ARCHIVED}
_ACTIVE_ATTEMPT = {RunStatus.QUEUED.value, RunStatus.RUNNING.value, "queued", "running"}
_DONE_ATTEMPT = {RunStatus.COMPLETED.value, "published"}
_RUN_STAGE = {
    RunType.ENGINEERING.value: JourneyStage.BUILD,
    RunType.DEPLOY.value: JourneyStage.DEPLOY,
    RunType.QA.value: JourneyStage.VERIFY,
}


def _aware(value: datetime | None) -> datetime | None:
    """Timestamps come from both naive and tz-aware columns; compare them all as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _min(values: Iterable[datetime | None]) -> datetime | None:
    present = [_aware(v) for v in values if v is not None]
    return min(present) if present else None  # type: ignore[type-var]


def _max(values: Iterable[datetime | None]) -> datetime | None:
    present = [_aware(v) for v in values if v is not None]
    return max(present) if present else None  # type: ignore[type-var]


def _preview(text: str | None) -> str | None:
    if not text:
        return None
    return text[:ERROR_PREVIEW_CHARS]


def stage_for(status: str, waiting_on: str) -> JourneyStage | None:
    """The stage a story is at, from what its own transitions recorded."""
    waiting = StoryWaitingOn(waiting_on)
    if waiting is not StoryWaitingOn.NONE:
        return _WAITING_STAGE[waiting]
    return _STATUS_STAGE.get(StoryStatus(status))


def _actor(run: Any) -> str | None:
    try:
        decision = ExecutorDecision.from_run_metadata(run.run_metadata)
    except ValueError:
        return None
    return decision.agent_type.value


def run_attempt(run: Any) -> JourneyAttempt:
    finished = run.completed_at if run.status not in _ACTIVE_ATTEMPT else None
    return JourneyAttempt(
        id=run.id,
        kind=run.type,
        status=run.status,
        task_id=run.task_id,
        actor=_actor(run),
        started_at=_aware(run.started_at or run.created_at),
        finished_at=_aware(finished),
        error=_preview(run.error_message),
    )


def install_attempt(task: Any) -> JourneyAttempt:
    operation = task.install_operation or {}
    state = operation.get("state") or task.status
    finished = (
        task.updated_at
        if task.status
        in {TaskStatus.DONE.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
        else None
    )
    return JourneyAttempt(
        id=task.id,
        kind="install",
        status=state,
        task_id=task.id,
        actor=None,
        started_at=_aware(task.created_at),
        finished_at=_aware(finished),
        error=_preview(operation.get("detail")),
    )


def _attempts_status(attempts: Sequence[JourneyAttempt]) -> StepStatus:
    if not attempts:
        return StepStatus.PENDING
    if any(a.status in _ACTIVE_ATTEMPT for a in attempts):
        return StepStatus.ACTIVE
    return StepStatus.DONE if attempts[-1].status in _DONE_ATTEMPT else StepStatus.FAILED


def _reached(story: Any, attempts_by_stage: dict[JourneyStage, list[JourneyAttempt]]) -> int:
    """Index of the stage the story is at; a failed story stops at its last attempted stage."""
    stage = stage_for(story.status, story.waiting_on)
    if stage is not None:
        return STAGES.index(stage)
    attempted = [STAGES.index(s) for s, attempts in attempts_by_stage.items() if attempts]
    return max(attempted, default=STAGES.index(JourneyStage.PLAN))


def package_refs(install: dict | None) -> list[PackageRef]:
    if not install:
        return []
    refs = [
        PackageRef(
            name=install["package"]["name"],
            version=install["package"]["version"],
            kind="package",
        )
    ]
    refs.extend(
        PackageRef(name=lib["name"], version=lib["version"], kind="library")
        for lib in install.get("libraries", [])
    )
    return refs


def build_steps(  # noqa: C901, PLR0912, PLR0915  # one linear walk over the eight stages
    story: Any,
    brief: Any | None,
    tasks: Sequence[Any],
    runs: Sequence[Any],
) -> list[JourneyStep]:
    """Lay one story's evidence out as the eight console stages."""
    ordered_runs = sorted(runs, key=lambda r: _aware(r.started_at or r.created_at))
    install_tasks = sorted(
        (t for t in tasks if t.type == TaskType.INSTALL.value), key=lambda t: _aware(t.created_at)
    )
    work_tasks = [t for t in tasks if t.type != TaskType.INSTALL.value]
    attempts: dict[JourneyStage, list[JourneyAttempt]] = defaultdict(list)
    for run in ordered_runs:
        attempts[_RUN_STAGE[run.type]].append(run_attempt(run))
    attempts[JourneyStage.INSTALL] = [install_attempt(t) for t in install_tasks]

    reached = _reached(story, attempts)
    status = StoryStatus(story.status)
    waiting = StoryWaitingOn(story.waiting_on)
    eng_runs = [r for r in ordered_runs if r.type == RunType.ENGINEERING.value]
    deploy_runs = [r for r in ordered_runs if r.type == RunType.DEPLOY.value]
    qa_runs = [r for r in ordered_runs if r.type == RunType.QA.value]

    steps: list[JourneyStep] = []
    for index, stage in enumerate(STAGES):
        stage_attempts = attempts.get(stage, [])
        facts: list[Fact] = []
        started: datetime | None = _min(a.started_at for a in stage_attempts)
        finished: datetime | None = (
            None
            if any(a.status in _ACTIVE_ATTEMPT for a in stage_attempts)
            else _max(a.finished_at for a in stage_attempts)
        )
        evidence = bool(stage_attempts)

        if stage is JourneyStage.BRIEF:
            evidence = brief is not None
            if brief is not None:
                started, finished = _aware(brief.created_at), _aware(brief.confirmed_at)
                facts.append(Fact(label="revision", value=str(brief.revision)))
                facts.append(Fact(label="brief", value=brief.id))
        elif stage is JourneyStage.PLAN:
            started = _aware(brief.confirmed_at) if brief and brief.confirmed_at else None
            started = started or _aware(story.created_at)
            finished = _min(t.created_at for t in tasks)
            evidence = bool(tasks)
            by_type: dict[str, int] = defaultdict(int)
            for task in tasks:
                by_type[task.type] += 1
            if by_type:
                facts.append(
                    Fact(
                        label="tasks",
                        value=", ".join(f"{k} ×{v}" for k, v in sorted(by_type.items())),
                    )
                )
        elif stage is JourneyStage.INSTALL:
            for task in install_tasks:
                for ref in package_refs(task.install):
                    facts.append(Fact(label=ref.kind, value=f"{ref.name} {ref.version}"))
        elif stage is JourneyStage.BUILD:
            done = sum(1 for t in work_tasks if t.status == TaskStatus.DONE.value)
            if work_tasks:
                facts.append(Fact(label="tasks done", value=f"{done} / {len(work_tasks)}"))
            failed = sum(1 for r in eng_runs if r.status == RunStatus.FAILED.value)
            if failed:
                facts.append(Fact(label="failed attempts", value=str(failed)))
        elif stage is JourneyStage.REVIEW:
            evidence = story.pr_number is not None
            started = _max(r.completed_at for r in eng_runs)
            finished = _min(r.started_at or r.created_at for r in deploy_runs)
            if story.pr_number is not None:
                facts.append(Fact(label="pull request", value=f"#{story.pr_number}"))
        elif stage is JourneyStage.DEPLOY:
            for run in deploy_runs[-1:]:
                url = (run.result or {}).get("deployed_url")
                if url:
                    facts.append(Fact(label="url", value=str(url)))
        elif stage is JourneyStage.VERIFY:
            if qa_runs:
                facts.append(Fact(label="qa runs", value=str(len(qa_runs))))
            decisions = len(story.unverified_decisions or [])
            if decisions:
                facts.append(Fact(label="unverified decisions", value=str(decisions)))
        elif stage is JourneyStage.LIVE:
            evidence = status is StoryStatus.COMPLETED
            if evidence:
                started = finished = _aware(story.status_entered_at or story.updated_at)

        if index < reached:
            step_status = (
                _attempts_status(stage_attempts)
                if stage_attempts
                else (StepStatus.DONE if evidence else StepStatus.SKIPPED)
            )
            if step_status is StepStatus.ACTIVE:
                step_status = StepStatus.DONE
        elif index == reached:
            if status is StoryStatus.COMPLETED:
                step_status = StepStatus.DONE
            elif waiting is not StoryWaitingOn.NONE or status in {
                StoryStatus.WAITING_HUMAN_REVIEW,
                StoryStatus.WAITING_USER_SECRET,
            }:
                step_status = StepStatus.WAITING
                facts.append(Fact(label="waiting on", value=waiting.value))
            elif status in {StoryStatus.FAILED, StoryStatus.ARCHIVED}:
                step_status = (
                    _attempts_status(stage_attempts) if stage_attempts else StepStatus.FAILED
                )
            else:
                step_status = StepStatus.ACTIVE
        else:
            step_status = (
                StepStatus.PENDING if status not in _TERMINAL_STORY else StepStatus.SKIPPED
            )
        if step_status in {StepStatus.PENDING, StepStatus.SKIPPED}:
            started = finished = None
        if step_status in {StepStatus.ACTIVE, StepStatus.WAITING}:
            finished = None

        steps.append(
            JourneyStep(
                stage=stage,
                status=step_status,
                started_at=started,
                finished_at=finished,
                attempts=stage_attempts,
                facts=facts,
            )
        )
    return steps


def finished_at(story: Any) -> datetime | None:
    if StoryStatus(story.status) in _TERMINAL_STORY:
        return _aware(story.status_entered_at or story.updated_at)
    return None


def journey_summary(story: Any, project_title: str) -> JourneySummary:
    return JourneySummary(
        story_id=story.id,
        title=story.title,
        project_id=story.project_id,
        project_title=project_title,
        status=story.status,
        waiting_on=story.waiting_on,
        current_stage=stage_for(story.status, story.waiting_on),
        created_at=_aware(story.created_at),
        updated_at=_aware(story.updated_at),
        finished_at=finished_at(story),
    )


def build_passports(
    projects: Sequence[Any],
    install_tasks: Sequence[Any],
    repositories: Sequence[Any],
    applications: Sequence[Any],
    ports: Sequence[Any],
    deployments: Sequence[Any],
) -> dict[uuid.UUID, ProductPassport]:
    """One passport per project from bulk-loaded rows; no per-project queries."""
    packages: dict[uuid.UUID, dict[str, PackageRef]] = defaultdict(dict)
    for task in sorted(install_tasks, key=lambda t: _aware(t.created_at)):
        if (task.install_operation or {}).get("state") != "published":
            continue
        for ref in package_refs(task.install):
            packages[task.project_id][f"{ref.kind}:{ref.name}"] = ref

    repo_project = {repo.id: repo.project_id for repo in repositories}
    port_by_app: dict[int, int] = {}
    for allocation in ports:
        if allocation.application_id is not None:
            port_by_app.setdefault(allocation.application_id, allocation.port)
    sha_by_app: dict[int, str | None] = {}
    for deployment in sorted(deployments, key=lambda d: _aware(d.deployed_at)):
        if deployment.application_id is not None:
            sha_by_app[deployment.application_id] = deployment.deployed_sha

    containers: dict[uuid.UUID, list[ContainerView]] = defaultdict(list)
    for app in sorted(applications, key=lambda a: (a.service_name, a.id)):
        project_id = repo_project.get(app.repo_id)
        if project_id is None:
            continue
        containers[project_id].append(
            ContainerView(
                name=app.service_name,
                status=app.status,
                placement=app.server_handle,
                port=port_by_app.get(app.id),
                reserved_ram_mb=app.reserved_ram_mb,
                response_time_ms=app.response_time_ms,
                uptime_pct_24h=app.uptime_pct_24h,
                deployed_sha=sha_by_app.get(app.id),
            )
        )

    passports: dict[uuid.UUID, ProductPassport] = {}
    for project in projects:
        config = project.config or {}
        passports[project.id] = ProductPassport(
            modules=list(config.get("modules") or []),
            packages=sorted(packages[project.id].values(), key=lambda p: (p.kind, p.name)),
            containers=containers[project.id],
            # Key names only: values stay encrypted in config and never leave the API here.
            user_secrets=sorted((config.get("secrets") or {}).keys()),
        )
    return passports


def placement_view(server: Any) -> PlacementView:
    role = "control" if (server.labels or {}).get("role") == CONTROL_HOST_ROLE else "product"
    return PlacementView(
        handle=server.handle,
        role=role,
        status=server.status,
        public_ip=server.public_ip,
        capacity_cpu=server.capacity_cpu,
        capacity_ram_mb=server.capacity_ram_mb,
        used_ram_mb=server.used_ram_mb,
        cpu_usage_pct=server.cpu_usage_pct,
        last_health_check=_aware(server.last_health_check),
    )


_TASK_SEVERITY = {
    TaskStatus.WAITING_HUMAN_REVIEW.value: Severity.CRITICAL,
    TaskStatus.FAILED.value: Severity.CRITICAL,
    TaskStatus.WAITING_RESOURCES.value: Severity.WARNING,
    TaskStatus.BLOCKED.value: Severity.INFO,
}
# A waiting task already names human review and resource waits; the story rows add
# only what no task carries.
_STORY_WAITS = {
    StoryWaitingOn.CI.value,
    StoryWaitingOn.DEPLOY.value,
    StoryWaitingOn.QA.value,
    StoryWaitingOn.USER_SECRET.value,
}
_CRITICAL_INCIDENTS = {IncidentType.SERVER_UNREACHABLE.value, IncidentType.SERVICE_DOWN.value}
_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def _task_detail(task: Any) -> str | None:
    metadata = task.failure_metadata or {}
    reason = metadata.get("reason")
    return _preview(str(reason)) if reason else None


def attention_items(
    titles: dict[uuid.UUID, str],
    tasks: Sequence[Any],
    stories: Sequence[Any],
    incidents: Sequence[Any],
    applications: Sequence[tuple[Any, uuid.UUID | None]],
    queue_issues: Sequence[str],
) -> list[AttentionItem]:
    """Everything that needs an operator, most severe and most recent first."""
    items: list[AttentionItem] = []
    for task in tasks:
        items.append(
            AttentionItem(
                kind="task",
                severity=_TASK_SEVERITY[task.status],
                title=f"Task {task.status.replace('_', ' ')}: {task.title}",
                detail=_task_detail(task),
                since=_aware(task.updated_at),
                project_id=task.project_id,
                project_title=titles.get(task.project_id),
                story_id=task.story_id,
                task_id=task.id,
                application_id=None,
                server_handle=None,
            )
        )
    for story in stories:
        failed = story.status == StoryStatus.FAILED.value
        items.append(
            AttentionItem(
                kind="story",
                severity=Severity.CRITICAL if failed else Severity.WARNING,
                title=(
                    f"Story failed: {story.title}"
                    if failed
                    else f"Story waits on {story.waiting_on.replace('_', ' ')}: {story.title}"
                ),
                detail=None,
                since=_aware(story.status_entered_at or story.updated_at),
                project_id=story.project_id,
                project_title=titles.get(story.project_id),
                story_id=story.id,
                task_id=None,
                application_id=None,
                server_handle=None,
            )
        )
    for incident in incidents:
        items.append(
            AttentionItem(
                kind="incident",
                severity=(
                    Severity.CRITICAL
                    if incident.incident_type in _CRITICAL_INCIDENTS
                    else Severity.WARNING
                ),
                title=f"Incident {incident.incident_type.replace('_', ' ')} ({incident.status})",
                detail=", ".join(incident.affected_services or []) or None,
                since=_aware(incident.detected_at),
                project_id=None,
                project_title=None,
                story_id=None,
                task_id=None,
                application_id=None,
                server_handle=incident.server_handle,
            )
        )
    for app, project_id in applications:
        down = app.status == ApplicationStatus.DOWN.value
        items.append(
            AttentionItem(
                kind="application",
                severity=Severity.CRITICAL if down else Severity.WARNING,
                title=f"{app.service_name} is {app.status}",
                detail=(f"response {app.response_time_ms} ms" if app.response_time_ms else None),
                since=_aware(app.last_health_check or app.updated_at),
                project_id=project_id,
                project_title=titles.get(project_id) if project_id else None,
                story_id=None,
                task_id=None,
                application_id=app.id,
                server_handle=app.server_handle,
            )
        )
    for issue in queue_issues:
        items.append(
            AttentionItem(
                kind="queue",
                severity=Severity.WARNING,
                title=issue,
                detail=None,
                since=None,
                project_id=None,
                project_title=None,
                story_id=None,
                task_id=None,
                application_id=None,
                server_handle=None,
            )
        )
    epoch = datetime.min.replace(tzinfo=UTC)
    items.sort(key=lambda i: i.since or epoch, reverse=True)
    items.sort(key=lambda i: _SEVERITY_ORDER[i.severity])
    return items


def median_lead_time_minutes(stories: Sequence[Any]) -> float | None:
    """Median minutes from story creation to completion over the given completed stories."""
    durations = [
        (_aware(s.status_entered_at) - _aware(s.created_at)).total_seconds() / 60
        for s in stories
        if s.status_entered_at is not None
    ]
    return round(median(durations), 1) if durations else None


# --- loaders -------------------------------------------------------------------------------


async def _project_passports(
    db: AsyncSession, projects: Sequence[Project]
) -> dict[uuid.UUID, ProductPassport]:
    ids = [p.id for p in projects]
    if not ids:
        return {}
    install_tasks = (
        await db.scalars(
            select(Task).where(Task.project_id.in_(ids), Task.type == TaskType.INSTALL.value)
        )
    ).all()
    repositories = (
        await db.scalars(select(Repository).where(Repository.project_id.in_(ids)))
    ).all()
    applications = (
        await db.scalars(
            select(Application).where(Application.repo_id.in_([r.id for r in repositories]))
        )
    ).all()
    app_ids = [a.id for a in applications]
    ports = (
        await db.scalars(select(PortAllocation).where(PortAllocation.application_id.in_(app_ids)))
    ).all()
    deployments = (
        await db.scalars(
            select(Deployment).where(
                Deployment.application_id.in_(app_ids),
                Deployment.result == DeploymentResult.SUCCESS.value,
            )
        )
    ).all()
    return build_passports(projects, install_tasks, repositories, applications, ports, deployments)


async def list_journeys(
    db: AsyncSession, limit: int, project_id: uuid.UUID | None = None
) -> list[JourneySummary]:
    query = (
        select(Story, Project.title)
        .join(Project, Project.id == Story.project_id)
        .order_by(Story.updated_at.desc(), Story.id.desc())
        .limit(limit)
    )
    if project_id is not None:
        query = query.where(Story.project_id == project_id)
    rows = (await db.execute(query)).all()
    return [journey_summary(story, title) for story, title in rows]


async def load_journey(db: AsyncSession, story_id: str) -> JourneyDetail | None:
    story = await db.get(Story, story_id)
    if story is None:
        return None
    project = await db.get(Project, story.project_id)
    brief = await db.scalar(select(ProductBrief).where(ProductBrief.story_id == story_id))
    tasks = (await db.scalars(select(Task).where(Task.story_id == story_id))).all()
    runs = (await db.scalars(select(Run).where(Run.story_id == story_id))).all()
    passports = await _project_passports(db, [project])
    content = brief.content if brief is not None else {}
    return JourneyDetail(
        summary=journey_summary(story, project.title),
        request=content.get("summary"),
        requirements=len(content["must_requirements"]) if "must_requirements" in content else None,
        pr_number=story.pr_number,
        steps=build_steps(story, brief, tasks, runs),
        passport=passports[project.id],
    )


async def build_topology(db: AsyncSession) -> TopologyResponse:
    servers = (await db.scalars(select(Server).order_by(Server.handle))).all()
    projects = (
        await db.scalars(
            select(Project)
            .where(Project.status != ProjectStatus.ARCHIVED.value)
            .order_by(Project.title)
        )
    ).all()
    passports = await _project_passports(db, projects)
    latest = dict(
        (
            await db.execute(
                select(Story.project_id, Story.id)
                .distinct(Story.project_id)
                .order_by(Story.project_id, Story.updated_at.desc())
            )
        ).all()
    )
    used = {c.placement for p in passports.values() for c in p.containers}
    return TopologyResponse(
        placements=[
            placement_view(s)
            for s in servers
            if s.handle in used or (s.labels or {}).get("role") == CONTROL_HOST_ROLE or s.is_managed
        ],
        products=[
            ProductView(
                project_id=p.id,
                title=p.title,
                slug=p.slug,
                status=p.status,
                latest_story_id=latest.get(p.id),
                passport=passports[p.id],
            )
            for p in projects
        ],
        platform_services=[],
    )


async def build_attention(db: AsyncSession, now: datetime | None = None) -> AttentionResponse:
    now = now or datetime.now(UTC)
    tasks = (
        await db.scalars(
            select(Task)
            .where(Task.status.in_(list(_TASK_SEVERITY)))
            .order_by(Task.updated_at.desc())
            .limit(ATTENTION_ROW_LIMIT)
        )
    ).all()
    stories = (
        await db.scalars(
            select(Story)
            .where(
                (Story.waiting_on.in_(list(_STORY_WAITS)))
                | (Story.status == StoryStatus.FAILED.value)
            )
            .order_by(Story.updated_at.desc())
            .limit(ATTENTION_ROW_LIMIT)
        )
    ).all()
    incidents = (
        await db.scalars(
            select(Incident)
            .where(Incident.status != IncidentStatus.RESOLVED.value)
            .order_by(Incident.detected_at.desc())
            .limit(ATTENTION_ROW_LIMIT)
        )
    ).all()
    sick = (
        await db.execute(
            select(Application, Repository.project_id)
            .outerjoin(Repository, Repository.id == Application.repo_id)
            .where(
                Application.status.in_(
                    [ApplicationStatus.DOWN.value, ApplicationStatus.DEGRADED.value]
                )
            )
            .limit(ATTENTION_ROW_LIMIT)
        )
    ).all()
    project_ids = {r.project_id for r in [*tasks, *stories]} | {pid for _, pid in sick if pid}
    titles = dict(
        (
            await db.execute(select(Project.id, Project.title).where(Project.id.in_(project_ids)))
        ).all()
    )
    queues = await get_queue_snapshot()

    run_counts = dict(
        (
            await db.execute(
                select(Run.status, func.count())
                .where(Run.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]))
                .group_by(Run.status)
            )
        ).all()
    )
    active_journeys = await db.scalar(
        select(func.count())
        .select_from(Story)
        .where(Story.status.not_in([s.value for s in _TERMINAL_STORY]))
    )
    live_products = await db.scalar(
        select(func.count(func.distinct(Repository.project_id)))
        .select_from(Application)
        .join(Repository, Repository.id == Application.repo_id)
        .where(Application.status == ApplicationStatus.RUNNING.value)
    )
    recent = (
        await db.scalars(
            select(Story).where(
                Story.status == StoryStatus.COMPLETED.value,
                Story.status_entered_at >= now - timedelta(days=7),
            )
        )
    ).all()
    return AttentionResponse(
        kpis=ConsoleKpis(
            active_journeys=active_journeys or 0,
            running_runs=run_counts.get(RunStatus.RUNNING.value, 0),
            queued_runs=run_counts.get(RunStatus.QUEUED.value, 0),
            live_products=live_products or 0,
            degraded_containers=len(sick),
            median_lead_time_minutes_7d=median_lead_time_minutes(recent),
        ),
        items=attention_items(titles, tasks, stories, incidents, sick, queues.issues),
    )
