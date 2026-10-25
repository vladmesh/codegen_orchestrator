"""Assemble the admin console read models from the rows the pipeline already writes.

Pure builders take loaded rows and return console DTOs; the `load_*` functions are the only
code that queries. Nothing here writes, and nothing invents a value a row does not carry: a
stage without evidence is `pending` or `skipped`, never guessed done.
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any
import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.deployment import DeploymentResult
from shared.contracts.dto.executor_decision import ExecutorDecision
from shared.contracts.dto.incident import IncidentStatus, IncidentType
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import DeployRunResult, QARunResult
from shared.contracts.dto.story import StoryStatus, StoryWaitingOn
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.worker_turn import AttemptTurnMetadata
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
    StoryLink,
    Task,
)

from .config import get_settings
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
    JourneyTimeline,
    PackageRef,
    PlacementView,
    PlatformServiceView,
    ProductPassport,
    ProductView,
    Severity,
    StepStatus,
    StoryLinkView,
    TaskBoardAttempt,
    TaskBoardColumn,
    TaskBoardEntry,
    TaskOrigin,
    TimelineStage,
    TimelineStageKind,
    TimelineStageState,
    TimelineTotalUnavailable,
    TimelineWait,
    TopologyResponse,
)
from .schemas.utc import assume_utc

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
_DONE_ATTEMPT = {RunStatus.COMPLETED.value, TaskStatus.DONE.value, "published"}
_RUN_STAGE = {
    RunType.ENGINEERING.value: JourneyStage.BUILD,
    RunType.DEPLOY.value: JourneyStage.DEPLOY,
    RunType.QA.value: JourneyStage.VERIFY,
}


def _aware(value: datetime | None) -> datetime | None:
    """Timestamps come from both naive and tz-aware columns; compare them all as UTC."""
    if value is None:
        return None
    return assume_utc(value)


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


_FINISHED_TASK = {TaskStatus.DONE.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}


def install_attempt(task: Any) -> JourneyAttempt:
    operation = task.install_operation or {}
    # A task the dispatcher has not claimed yet has no operation: it is queued, not failed.
    state = operation.get("state") or (task.status if task.status in _FINISHED_TASK else "queued")
    finished = task.updated_at if task.status in _FINISHED_TASK else None
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
    """Released catalog components; a product-local source has no release to name."""
    if not install or install.get("route") == "package_source":
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
            # An in-progress story already counts as building while its install still runs.
            if step_status is StepStatus.ACTIVE and stage is not JourneyStage.INSTALL:
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


TASK_COLUMN: dict[TaskStatus, TaskBoardColumn] = {
    TaskStatus.BACKLOG: TaskBoardColumn.BACKLOG,
    TaskStatus.TODO: TaskBoardColumn.BACKLOG,
    TaskStatus.IN_DEV: TaskBoardColumn.IN_DEVELOPMENT,
    TaskStatus.BLOCKED: TaskBoardColumn.IN_DEVELOPMENT,
    TaskStatus.WAITING_HUMAN_REVIEW: TaskBoardColumn.IN_DEVELOPMENT,
    TaskStatus.WAITING_RESOURCES: TaskBoardColumn.IN_DEVELOPMENT,
    TaskStatus.IN_CI: TaskBoardColumn.CI,
    TaskStatus.TESTING: TaskBoardColumn.CI,
    TaskStatus.DONE: TaskBoardColumn.DONE,
    TaskStatus.FAILED: TaskBoardColumn.DONE,
    TaskStatus.CANCELLED: TaskBoardColumn.DONE,
}


def task_origin(task: Any) -> TaskOrigin:
    """A fix task carries the failure that created it; any other task was planned."""
    metadata = task.failure_metadata or {}
    if "ci_failure" in metadata:
        return TaskOrigin.CI_FIX
    if "qa_failure" in metadata:
        return TaskOrigin.QA_FIX
    return TaskOrigin.PLANNED


def task_board_attempt(number: int, run: Any) -> TaskBoardAttempt:
    """Raises when the run metadata does not validate as `AttemptTurnMetadata`."""
    attempt = run_attempt(run)
    return TaskBoardAttempt(
        number=number,
        run_id=attempt.id,
        status=attempt.status,
        worker_id=AttemptTurnMetadata.from_run_metadata(run.run_metadata).worker_id,
        actor=attempt.actor,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )


def build_task_board(tasks: Sequence[Any], runs: Sequence[Any]) -> list[TaskBoardEntry]:
    """Every task of a story, oldest first, with its engineering runs as numbered attempts."""
    runs_by_task: dict[str, list[Any]] = defaultdict(list)
    for run in sorted(runs, key=lambda r: _aware(r.started_at or r.created_at)):
        if run.type == RunType.ENGINEERING.value and run.task_id is not None:
            runs_by_task[run.task_id].append(run)

    board: list[TaskBoardEntry] = []
    for task in sorted(tasks, key=lambda t: (_aware(t.created_at), t.id)):
        attempts = [
            task_board_attempt(number, run)
            for number, run in enumerate(runs_by_task[task.id], start=1)
        ]
        status = TaskStatus(task.status)
        board.append(
            TaskBoardEntry(
                task_id=task.id,
                title=task.title,
                type=task.type,
                status=status,
                column=TASK_COLUMN[status],
                origin=task_origin(task),
                attempts=attempts,
                worker_id=attempts[-1].worker_id if attempts else None,
                attempt_count=len(attempts),
            )
        )
    return board


_TIMELINE_RUN_KIND = {
    RunType.ENGINEERING.value: TimelineStageKind.ENGINEERING,
    RunType.DEPLOY.value: TimelineStageKind.DEPLOY,
    RunType.QA.value: TimelineStageKind.QA,
}
_TIMELINE_LABEL = {
    TimelineStageKind.ARCHITECT: "architect",
    TimelineStageKind.SCAFFOLD: "scaffold",
    TimelineStageKind.ENGINEERING: "engineering",
    TimelineStageKind.CI: "CI",
    TimelineStageKind.MERGE: "merge",
    TimelineStageKind.DEPLOY: "deploy",
    TimelineStageKind.QA: "QA",
}
_NUMBERED_KINDS = {
    TimelineStageKind.ENGINEERING,
    TimelineStageKind.CI,
    TimelineStageKind.DEPLOY,
    TimelineStageKind.QA,
}
_KIND_ORDER = {kind: index for index, kind in enumerate(TimelineStageKind)}

_Span = tuple[TimelineStageKind, datetime | None, datetime | None]


def _recorded_instant(value: object) -> datetime | None:
    """A time recorded as an ISO-8601 string, as UTC; None when absent. Anything else raises."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"recorded time {value!r} is not an ISO-8601 string")
    return assume_utc(datetime.fromisoformat(value))


def _story_spans(story: Any, brief: Any | None, project: Any, runs: Sequence[Any]) -> list[_Span]:
    spans: list[_Span] = [
        (
            TimelineStageKind.ARCHITECT,
            _aware(brief.confirmed_at) if brief is not None else None,
            _aware(brief.coverage_admitted_at) if brief is not None else None,
        )
    ]
    config = project.config or {}
    scaffold_start = _recorded_instant(config.get("scaffold_started_at"))
    scaffold_end = _recorded_instant(config.get("scaffold_finished_at"))
    # A project is scaffolded once, for its first story; a later story did not wait on it.
    if scaffold_start is None or scaffold_start >= _aware(story.created_at):
        # A rerun stamps a new start and leaves the earlier attempt's finish until it ends.
        if scaffold_start and scaffold_end and scaffold_end < scaffold_start:
            scaffold_end = None
        spans.append((TimelineStageKind.SCAFFOLD, scaffold_start, scaffold_end))

    timeline = story.generated_product_timeline or {}
    for ci_run in timeline.get("ci_runs") or []:
        spans.append(
            (
                TimelineStageKind.CI,
                _recorded_instant(ci_run.get("started_at")),
                _recorded_instant(ci_run.get("completed_at")),
            )
        )
    merged = _recorded_instant((timeline.get("pull_request") or {}).get("merged_at"))
    spans.append((TimelineStageKind.MERGE, merged, merged))
    for run in sorted(runs, key=lambda r: _aware(r.created_at)):
        kind = _TIMELINE_RUN_KIND[run.type]
        spans.append((kind, _aware(run.started_at), _aware(run.completed_at)))
    return spans


def _timeline_stage(
    kind: TimelineStageKind, number: int, started: datetime | None, finished: datetime | None
) -> TimelineStage:
    label = _TIMELINE_LABEL[kind]
    if kind in _NUMBERED_KINDS:
        label = f"{label} {number}"
    if started is None:
        state, duration = TimelineStageState.UNRECORDED, None
    elif finished is None:
        state, duration = TimelineStageState.RUNNING, None
    else:
        state, duration = TimelineStageState.DONE, (finished - started).total_seconds()
    return TimelineStage(
        kind=kind,
        label=label,
        started_at=started,
        finished_at=finished,
        duration_seconds=duration,
        state=state,
    )


def _timeline_waits(timed: Sequence[TimelineStage]) -> list[TimelineWait]:
    """Positive gaps between the latest end so far and each next start.

    A running stage has no end yet, so it covers every later start.
    """
    waits: list[TimelineWait] = []
    latest: TimelineStage | None = None
    open_stage = False
    for stage in timed:
        if latest is not None and not open_stage and stage.started_at > latest.finished_at:
            waits.append(
                TimelineWait(
                    after=latest.label,
                    before=stage.label,
                    seconds=(stage.started_at - latest.finished_at).total_seconds(),
                )
            )
        if stage.finished_at is None:
            open_stage = True
        elif latest is None or stage.finished_at > latest.finished_at:
            latest = stage
    return waits


def _first_qa_pass(runs: Sequence[Any]) -> datetime | None:
    """The earliest recorded end of a passed QA run; every completed QA result must validate."""
    passes = [
        _aware(run.completed_at)
        for run in runs
        if run.type == RunType.QA.value
        and run.status == RunStatus.COMPLETED.value
        and QARunResult.model_validate(run.result).qa_outcome is QAOutcome.PASSED
        and run.completed_at is not None
    ]
    return min(passes, default=None)


def build_timeline(
    story: Any, brief: Any | None, project: Any, runs: Sequence[Any]
) -> JourneyTimeline:
    """Each stage's recorded start and end, the waits between stages, and the story's total.

    A missing time leaves its stage `unrecorded`; a recorded time that is not ISO-8601 raises.
    """
    spans = _story_spans(story, brief, project, runs)
    by_kind: dict[TimelineStageKind, list[_Span]] = defaultdict(list)
    for span in spans:
        by_kind[span[0]].append(span)
    stages: list[TimelineStage] = []
    for kind, kind_spans in by_kind.items():
        # A stable sort keeps unrecorded spans of one kind in recorded order, after the timed.
        kind_spans.sort(key=lambda s: (s[1] is None, s[1] or datetime.min.replace(tzinfo=UTC)))
        stages.extend(
            _timeline_stage(kind, number, started, finished)
            for number, (_, started, finished) in enumerate(kind_spans, start=1)
        )
    timed = sorted(
        (s for s in stages if s.started_at is not None),
        key=lambda s: (s.started_at, _KIND_ORDER[s.kind]),
    )
    unrecorded = sorted(
        (s for s in stages if s.started_at is None), key=lambda s: _KIND_ORDER[s.kind]
    )

    passed_at = _first_qa_pass(runs)
    total: float | None = None
    if brief is None:
        unavailable: TimelineTotalUnavailable | None = TimelineTotalUnavailable.NO_BRIEF
    elif brief.confirmed_at is None:
        unavailable = TimelineTotalUnavailable.BRIEF_NOT_CONFIRMED
    elif passed_at is None:
        unavailable = TimelineTotalUnavailable.NO_PASSED_QA
    else:
        unavailable = None
        total = (passed_at - _aware(brief.confirmed_at)).total_seconds()
    return JourneyTimeline(
        stages=[*timed, *unrecorded],
        waits=_timeline_waits(timed),
        total_seconds=total,
        total_unavailable=unavailable,
    )


def link_view(link: Any) -> StoryLinkView:
    return StoryLinkView(
        id=link.id,
        kind=link.kind,
        title=link.title,
        url=link.url,
        created_at=_aware(link.created_at),
    )


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


def split_secrets(
    secret_keys: Iterable[str], user_secret_keys: Collection[str] | None
) -> tuple[list[str], list[str], list[str]]:
    """Sorted (user, platform, unclassified) key names of a project's config secrets.

    `user_secret_keys` is the latest recorded contract classification, None when
    no deploy recorded one: every key is then unclassified.
    """
    keys = sorted(secret_keys)
    if user_secret_keys is None:
        return [], [], keys
    return (
        [k for k in keys if k in user_secret_keys],
        [k for k in keys if k not in user_secret_keys],
        [],
    )


def build_passports(
    projects: Sequence[Any],
    install_tasks: Sequence[Any],
    repositories: Sequence[Any],
    applications: Sequence[Any],
    ports: Sequence[Any],
    deployments: Sequence[Any],
    deploy_runs: Sequence[Any],
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

    classification: dict[uuid.UUID, list[str]] = {}
    for run in sorted(deploy_runs, key=lambda r: _aware(r.created_at)):
        result = DeployRunResult.model_validate(run.result)
        if result.secrets_classified:
            classification[run.project_id] = result.user_secret_keys

    passports: dict[uuid.UUID, ProductPassport] = {}
    for project in projects:
        config = project.config or {}
        # Key names only: values stay encrypted in config and never leave the API here.
        user, platform, unclassified = split_secrets(
            (config.get("secrets") or {}).keys(), classification.get(project.id)
        )
        passports[project.id] = ProductPassport(
            modules=list(config.get("modules") or []),
            packages=sorted(packages[project.id].values(), key=lambda p: (p.kind, p.name)),
            containers=containers[project.id],
            user_secrets=user,
            platform_secrets=platform,
            unclassified_secrets=unclassified,
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
    ci_stories: Sequence[Any],
) -> list[AttentionItem]:
    """Everything that needs an operator, most severe and most recent first."""
    items: list[AttentionItem] = ci_attention_items(titles, ci_stories)
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


_CI_QUIET_STORY = {StoryStatus.COMPLETED, StoryStatus.ARCHIVED}


def _latest_ci_run(story: Any) -> dict | None:
    """The story's latest recorded CI run; raises when `ci_runs` is not a list of objects.

    Runs without a recorded start predate start capture, so they order first, by list position.
    """
    timeline = story.generated_product_timeline
    if not isinstance(timeline, dict):
        raise ValueError(f"story {story.id} generated_product_timeline is not an object")
    runs = timeline["ci_runs"]
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise ValueError(f"story {story.id} ci_runs is not a list of objects")
    epoch = datetime.min.replace(tzinfo=UTC)
    ordered = sorted(
        runs,
        key=lambda run: (
            run.get("started_at") is not None,
            _recorded_instant(run.get("started_at")) or epoch,
        ),
    )
    return ordered[-1] if ordered else None


def ci_attention_items(titles: dict[uuid.UUID, str], stories: Sequence[Any]) -> list[AttentionItem]:
    """One item per open story whose latest CI run on its pull request failed."""
    items: list[AttentionItem] = []
    for story in stories:
        if story.status in _CI_QUIET_STORY:
            continue
        latest = _latest_ci_run(story)
        if latest is None or latest["conclusion"] != "failure":
            continue
        if story.pr_number is None:
            raise ValueError(f"story {story.id} has CI runs but no pull request number")
        completed = _recorded_instant(latest.get("completed_at"))
        items.append(
            AttentionItem(
                kind="ci",
                severity=Severity.WARNING,
                title=f"CI failed on PR #{story.pr_number}: {story.title}",
                detail=_preview(", ".join(job["name"] for job in latest["failed_jobs"])),
                since=completed or _aware(story.updated_at),
                project_id=story.project_id,
                project_title=titles.get(story.project_id),
                story_id=story.id,
                task_id=None,
                application_id=None,
                server_handle=None,
            )
        )
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
    deploy_runs = (
        await db.scalars(
            select(Run).where(
                Run.project_id.in_(ids),
                Run.type == RunType.DEPLOY.value,
                Run.result["user_secret_keys"].as_string().is_not(None),
            )
        )
    ).all()
    return build_passports(
        projects, install_tasks, repositories, applications, ports, deployments, deploy_runs
    )


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
    links = (
        await db.scalars(
            select(StoryLink)
            .where(StoryLink.story_id == story_id)
            .order_by(StoryLink.created_at, StoryLink.id)
        )
    ).all()
    passports = await _project_passports(db, [project])
    content = brief.content if brief is not None else {}
    return JourneyDetail(
        summary=journey_summary(story, project.title),
        request=content.get("summary"),
        requirements=len(content["must_requirements"]) if "must_requirements" in content else None,
        pr_number=story.pr_number,
        steps=build_steps(story, brief, tasks, runs),
        passport=passports[project.id],
        links=[link_view(link) for link in links],
        task_board=build_task_board(tasks, runs),
        timeline=build_timeline(story, brief, project, runs),
    )


_PLATFORM_PROBE_TTL = timedelta(seconds=60)
_platform_probe_cache: dict[str, tuple[datetime, str]] = {}


async def probe_platform_service(url: str, now: datetime) -> str:
    """`up` when the service's edge answers below 5xx, `down` on 5xx, `unreachable` otherwise.

    Any answer below 5xx (401 without a key, 404 on the bare base URL) proves the service
    behind the edge is serving; a 502/503 is the edge reporting it gone.
    """
    cached = _platform_probe_cache.get(url)
    if cached and now - cached[0] < _PLATFORM_PROBE_TTL:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
        status = "up" if response.status_code < httpx.codes.INTERNAL_SERVER_ERROR else "down"
    except httpx.HTTPError:
        status = "unreachable"
    _platform_probe_cache[url] = (now, status)
    return status


def platform_service_views(
    declared: dict[str, str],
    statuses: dict[str, str],
    used_by: dict[uuid.UUID, list[str]],
) -> list[PlatformServiceView]:
    """Declared services plus any a deployed product uses, each with the products using it."""
    names = set(declared) | {name for names in used_by.values() for name in names}
    return [
        PlatformServiceView(
            name=name,
            status=statuses.get(name, "undeclared"),
            products=sorted((pid for pid, names in used_by.items() if name in names), key=str),
        )
        for name in sorted(names)
    ]


async def _platform_services(db: AsyncSession, now: datetime) -> list[PlatformServiceView]:
    latest = (
        await db.execute(
            select(Deployment.project_id, Deployment.deployment_info)
            .where(Deployment.result == DeploymentResult.SUCCESS.value)
            .distinct(Deployment.project_id)
            .order_by(Deployment.project_id, Deployment.deployed_at.desc())
        )
    ).all()
    used_by = {
        pid: list((info or {}).get("platform_services") or [])
        for pid, info in latest
        if (info or {}).get("platform_services")
    }
    declared = get_settings().get_platform_services()
    statuses = {name: await probe_platform_service(url, now) for name, url in declared.items()}
    return platform_service_views(declared, statuses, used_by)


async def build_topology(db: AsyncSession, now: datetime | None = None) -> TopologyResponse:
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
        platform_services=await _platform_services(db, now or datetime.now(UTC)),
    )


def scope_attention(
    items: list[AttentionItem], since: datetime | None, project_id: uuid.UUID | None
) -> list[AttentionItem]:
    """Narrow the feed to one project and to items that started at or after `since`.

    A project scope drops platform-wide items (incidents, queues); `since` keeps undated items,
    which describe the present moment.
    """
    if project_id is not None:
        items = [i for i in items if i.project_id == project_id]
    if since is not None:
        cutoff = _aware(since)
        items = [i for i in items if i.since is None or i.since >= cutoff]
    return items


async def build_attention(
    db: AsyncSession,
    now: datetime | None = None,
    since: datetime | None = None,
    project_id: uuid.UUID | None = None,
) -> AttentionResponse:
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
    ci_stories = (
        await db.scalars(
            select(Story)
            .where(
                Story.status.not_in([s.value for s in _CI_QUIET_STORY]),
                Story.generated_product_timeline["ci_runs"].as_string().is_not(None),
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
    project_ids = {r.project_id for r in [*tasks, *stories, *ci_stories]} | {
        pid for _, pid in sick if pid
    }
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
    active_query = (
        select(func.count())
        .select_from(Story)
        .where(Story.status.not_in([s.value for s in _TERMINAL_STORY]))
    )
    if project_id is not None:
        active_query = active_query.where(Story.project_id == project_id)
    if since is not None:
        active_query = active_query.where(Story.updated_at >= _aware(since))
    active_journeys = await db.scalar(active_query)
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
    items = scope_attention(
        attention_items(titles, tasks, stories, incidents, sick, queues.issues, ci_stories),
        since,
        project_id,
    )
    return AttentionResponse(
        kpis=ConsoleKpis(
            active_journeys=active_journeys or 0,
            running_runs=run_counts.get(RunStatus.RUNNING.value, 0),
            queued_runs=run_counts.get(RunStatus.QUEUED.value, 0),
            live_products=live_products or 0,
            degraded_containers=sum(1 for i in items if i.kind == "application"),
            median_lead_time_minutes_7d=median_lead_time_minutes(recent),
        ),
        items=items,
    )
