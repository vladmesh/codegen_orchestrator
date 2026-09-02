"""The Product Brief boundary: confirmed intent, one planner, one admission.

This router owns the coverage-to-dispatch boundary of a brief-backed story. The
shape is deliberately small:

* a brief is created and confirmed, and never edited — a change is a new
  revision, so an architect planning against revision N cannot have the ground
  move under it;
* exactly one architect owns an incomplete plan at a time, through
  claim / heartbeat / finish on the brief row;
* every must-requirement gets one disposition — a task that covers it, or a
  reason it was returned;
* and one durable, idempotent step, `POST /{id}/admit`, decides in a single
  transaction on locked rows whether the plan is complete and, if so, releases
  the tasks planned under that attempt.

`admit` is the *only* thing that sets `Task.dispatch_admitted`. The gate that
reads it is one condition in `engineering_dispatch_admission`, not a second
admission surface here.

Every write takes the brief row with `SELECT ... FOR UPDATE` first and any Task
row after it; see `_product_brief_helpers` for why that order is the safe one.
"""

from datetime import UTC, datetime
from hashlib import sha256
import json
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.product_brief import (
    ProductBriefAdmissionOutcome,
    ProductBriefAdmissionRead,
    ProductBriefConfirm,
    ProductBriefCreate,
    ProductBriefPlanningAttemptCommand,
    ProductBriefPlanningAttemptOutcome,
    ProductBriefPlanningAttemptRead,
    ProductBriefRead,
    ProductBriefStoryBind,
    RequirementCoverageCreate,
    RequirementCoverageRead,
)
from shared.models import ProductBrief, Project, RequirementCoverage, Story, Task

from ..database import get_async_session
from ..dependencies import _optional_bearer_scheme, is_internal_service
from ._product_brief_helpers import (
    attempt_heartbeat_is_fresh,
    load_brief_for_update,
    require_active_attempt,
    void_superseded_plan,
)
from .projects_guards import check_project_access

logger = structlog.get_logger()

router = APIRouter(prefix="/product-briefs", tags=["product-briefs"])


def _content_fingerprint(content: dict) -> str:
    """A stable hash of a brief document, so "the same content" is one question."""
    return sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _authorize(
    project_id: uuid.UUID,
    telegram_id: int | None,
    db: AsyncSession,
    internal: bool,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    """May this caller reach this project's brief?

    The project row is read *without* a lock, unlike every project config
    writer, and deliberately: nothing here writes the project, the access
    decision reads `owner_id` which no brief operation can change, and taking
    the project row while holding the brief would put a project below a brief in
    one place and above it in another.
    """
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    await check_project_access(
        project, telegram_id, db, is_internal=internal, credentials=credentials
    )


def _planning_read(
    brief: ProductBrief, outcome: ProductBriefPlanningAttemptOutcome
) -> ProductBriefPlanningAttemptRead:
    if brief.story_id is None:
        # Unreachable: every planning route requires the binding first.
        raise RuntimeError(f"Product Brief {brief.id} has no story")
    return ProductBriefPlanningAttemptRead(
        brief_id=brief.id,
        story_id=brief.story_id,
        outcome=outcome,
        planning_attempt_id=brief.planning_attempt_id,
        planning_attempt_heartbeat_at=brief.planning_attempt_heartbeat_at,
    )


def _require_planning_subject(brief: ProductBrief) -> str:
    """A brief can only be planned once it is confirmed and bound to a story."""
    if brief.confirmed_at is None or brief.story_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="planning requires a confirmed Product Brief bound to a story",
        )
    return brief.story_id


def _must_requirement_ids(brief: ProductBrief) -> set[str]:
    return {requirement["id"] for requirement in brief.content["must_requirements"]}


def _task_is_in_plan(task: Task, brief: ProductBrief, story_id: str) -> bool:
    """Is this task still a member of the plan the brief is being admitted for?

    Plan membership is project, story and attempt together — the same three
    fields the task update route freezes while a task is unadmitted. Coverage
    asks it before recording a disposition, and `admit` asks it again over every
    row it counts, so the durable record is never written over an
    inconsistency.
    """
    return (
        task.project_id == brief.project_id
        and task.story_id == story_id
        and task.planning_attempt_id == brief.planning_attempt_id
    )


# --- the brief itself ---------------------------------------------------------


@router.post("/", response_model=ProductBriefRead, status_code=status.HTTP_201_CREATED)
async def create_product_brief(
    body: ProductBriefCreate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefRead:
    """Open the next revision of this project's brief.

    There is no update path: confirmed content is never rewritten, so a change
    to what the user asked for arrives here as revision N+1.
    """
    await _authorize(body.project_id, x_telegram_id, db, internal, credentials)
    content = body.content.model_dump(mode="json")
    existing = (
        await db.execute(select(ProductBrief).where(ProductBrief.request_id == body.request_id))
    ).scalar_one_or_none()
    if existing is not None:
        # A retry of the same creation. Same content, same revision; different
        # content under the same key is a caller bug, not a second revision.
        if (
            existing.project_id != body.project_id
            or existing.title != body.title
            or _content_fingerprint(existing.content) != _content_fingerprint(content)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="request_id already names a different Product Brief",
            )
        return ProductBriefRead.model_validate(existing, from_attributes=True)
    revision = (
        await db.scalar(
            select(func.max(ProductBrief.revision)).where(
                ProductBrief.project_id == body.project_id
            )
        )
        or 0
    ) + 1
    brief = ProductBrief(
        id=f"brief-{secrets.token_hex(12)}",
        project_id=body.project_id,
        revision=revision,
        title=body.title,
        content=content,
        request_id=body.request_id,
    )
    db.add(brief)
    try:
        await db.commit()
    except IntegrityError as clash:
        # Two creations raced for the same next revision. `(project_id,
        # revision)` is unique, so one of them lost; say so as a retryable
        # conflict rather than letting a broken transaction surface as a 500.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another Product Brief revision was opened concurrently; retry",
        ) from clash
    await db.refresh(brief)
    logger.info("product_brief_created", brief_id=brief.id, revision=brief.revision)
    return ProductBriefRead.model_validate(brief, from_attributes=True)


@router.post("/{brief_id}/confirm", response_model=ProductBriefRead)
async def confirm_product_brief(
    brief_id: str,
    body: ProductBriefConfirm,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefRead:
    """Freeze this revision. The content is echoed back, never replaced."""
    brief = await load_brief_for_update(brief_id, db)
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    presented = _content_fingerprint(body.content.model_dump(mode="json"))
    if _content_fingerprint(brief.content) != presented:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="confirmation content does not match the stored revision",
        )
    if brief.confirmed_at is not None:
        if brief.confirmation_request_id != body.request_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Product Brief is already confirmed",
            )
        return ProductBriefRead.model_validate(brief, from_attributes=True)
    brief.confirmed_at = datetime.now(UTC)
    brief.confirmation_request_id = body.request_id
    await db.commit()
    await db.refresh(brief)
    logger.info("product_brief_confirmed", brief_id=brief.id)
    return ProductBriefRead.model_validate(brief, from_attributes=True)


@router.post("/{brief_id}/story", response_model=ProductBriefRead)
async def bind_product_brief_story(
    brief_id: str,
    body: ProductBriefStoryBind,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefRead:
    """Bind a confirmed brief to the story its plan is built in. Idempotent."""
    brief = await load_brief_for_update(brief_id, db)
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    if brief.confirmed_at is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="an unconfirmed Product Brief cannot be planned",
        )
    if brief.story_id is not None:
        if brief.story_id != body.story_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Product Brief is already bound to another story",
            )
        return ProductBriefRead.model_validate(brief, from_attributes=True)
    story = (await db.execute(select(Story).where(Story.id == body.story_id))).scalar_one_or_none()
    if story is None or story.project_id != brief.project_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="story must exist and belong to the Product Brief's project",
        )
    already = (
        await db.execute(select(ProductBrief.id).where(ProductBrief.story_id == body.story_id))
    ).scalar_one_or_none()
    if already is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="story is already backed by another Product Brief",
        )
    brief.story_id = body.story_id
    await db.commit()
    await db.refresh(brief)
    logger.info("product_brief_bound", brief_id=brief.id, story_id=brief.story_id)
    return ProductBriefRead.model_validate(brief, from_attributes=True)


@router.get("/by-story/{story_id}", response_model=ProductBriefRead)
async def get_product_brief_by_story(
    story_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefRead:
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.story_id == story_id))
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Story {story_id} is not backed by a Product Brief",
        )
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    return ProductBriefRead.model_validate(brief, from_attributes=True)


@router.get("/{brief_id}", response_model=ProductBriefRead)
async def get_product_brief(
    brief_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefRead:
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.id == brief_id))
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product Brief {brief_id} not found"
        )
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    return ProductBriefRead.model_validate(brief, from_attributes=True)


# --- one live architect per incomplete plan -----------------------------------


@router.post("/{brief_id}/planning-attempts/claim", response_model=ProductBriefPlanningAttemptRead)
async def claim_planning_attempt(
    brief_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefPlanningAttemptRead:
    """Take ownership of this brief's incomplete plan, if it is free to take.

    Two architects that claim at the same moment queue on the brief row, so one
    gets `CLAIMED` with a fresh attempt id and the other re-reads the row the
    winner committed and gets `IN_PROGRESS`. A claim whose heartbeat has gone
    stale is taken over — with a *new* attempt id, which supersedes the previous
    owner: its tasks are no longer in any admission's release set and its
    coverage no longer counts.

    Superseding is therefore also *voiding*. The same transaction that mints the
    new id cancels the superseded attempt's unadmitted tasks and deletes its
    dispositions (`void_superseded_plan`), because a task nobody will ever
    release is not merely useless — it blocks its story's completion for good.
    One transaction, so a takeover either voids the old plan and opens the new
    one, or does neither.
    """
    brief = await load_brief_for_update(brief_id, db)
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    story_id = _require_planning_subject(brief)
    if brief.coverage_admitted_at is not None:
        return _planning_read(brief, ProductBriefPlanningAttemptOutcome.ALREADY_ADMITTED)
    now = datetime.now(UTC)
    if brief.planning_attempt_active and attempt_heartbeat_is_fresh(brief, now):
        return _planning_read(brief, ProductBriefPlanningAttemptOutcome.IN_PROGRESS)
    superseded_attempt_id = brief.planning_attempt_id
    brief.planning_attempt_id = f"plan-{secrets.token_hex(12)}"
    brief.planning_attempt_active = True
    brief.planning_attempt_heartbeat_at = now
    voided, uncancellable = await void_superseded_plan(
        brief=brief,
        story_id=story_id,
        superseded_attempt_id=superseded_attempt_id,
        db=db,
    )
    await db.commit()
    await db.refresh(brief)
    logger.info(
        "product_brief_planning_claimed",
        brief_id=brief.id,
        planning_attempt_id=brief.planning_attempt_id,
        superseded_planning_attempt_id=superseded_attempt_id,
        voided_task_ids=voided,
        uncancellable_task_ids=uncancellable,
    )
    return _planning_read(brief, ProductBriefPlanningAttemptOutcome.CLAIMED)


@router.post(
    "/{brief_id}/planning-attempts/heartbeat", response_model=ProductBriefPlanningAttemptRead
)
async def heartbeat_planning_attempt(
    brief_id: str,
    body: ProductBriefPlanningAttemptCommand,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefPlanningAttemptRead:
    """Prove the owning architect is alive, so its claim is not taken over."""
    brief = await load_brief_for_update(brief_id, db)
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    _require_planning_subject(brief)
    require_active_attempt(brief, body.planning_attempt_id)
    brief.planning_attempt_heartbeat_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(brief)
    return _planning_read(brief, ProductBriefPlanningAttemptOutcome.CLAIMED)


@router.post("/{brief_id}/planning-attempts/finish", response_model=ProductBriefPlanningAttemptRead)
async def finish_planning_attempt(
    brief_id: str,
    body: ProductBriefPlanningAttemptCommand,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefPlanningAttemptRead:
    """Give up ownership without admitting, so the next architect need not wait.

    A planner that failed closes its own attempt here; nothing is released,
    because releasing is what `admit` does and only `admit` does.
    """
    brief = await load_brief_for_update(brief_id, db)
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    _require_planning_subject(brief)
    if brief.planning_attempt_id != body.planning_attempt_id:
        # Already taken over, so this caller has nothing left to give up.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product Brief planning attempt has already been superseded",
        )
    brief.planning_attempt_active = False
    await db.commit()
    await db.refresh(brief)
    return _planning_read(brief, ProductBriefPlanningAttemptOutcome.RELEASED)


# --- requirement dispositions --------------------------------------------------


@router.put("/{brief_id}/coverage/{requirement_id}", response_model=RequirementCoverageRead)
async def record_requirement_coverage(
    brief_id: str,
    requirement_id: str,
    body: RequirementCoverageCreate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> RequirementCoverageRead:
    """Record how the owning architect disposed of one must-requirement."""
    if body.requirement_id != requirement_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="requirement id in the path and the body disagree",
        )
    brief = await load_brief_for_update(brief_id, db)
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    story_id = _require_planning_subject(brief)
    if brief.coverage_admitted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product Brief coverage is already admitted",
        )
    require_active_attempt(brief, body.planning_attempt_id)
    if requirement_id not in _must_requirement_ids(brief):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unknown Product Brief must-requirement",
        )
    if body.task_id is not None:
        task = (
            await db.execute(select(Task).where(Task.id == body.task_id).with_for_update())
        ).scalar_one_or_none()
        # Story, project and attempt — the whole of a task's plan membership.
        # Project is not implied by story: the supported task update route can
        # set `project_id`, and a disposition approved under this brief's
        # project must not end up releasing work charged to another one.
        if task is None or not _task_is_in_plan(task, brief, story_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="a covering task must be planned under this planning attempt",
            )
    coverage = (
        await db.execute(
            select(RequirementCoverage)
            .where(
                RequirementCoverage.brief_id == brief.id,
                RequirementCoverage.requirement_id == requirement_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if coverage is None:
        coverage = RequirementCoverage(brief_id=brief.id, requirement_id=requirement_id)
        db.add(coverage)
    # An overwrite is the supported path for a takeover: the replacement planner
    # re-disposes of every requirement under its own attempt, and the row it
    # leaves behind names that attempt, so the stale disposition stops counting.
    coverage.planning_attempt_id = body.planning_attempt_id
    coverage.task_id = body.task_id
    coverage.returned_reason = body.returned_reason
    await db.commit()
    await db.refresh(coverage)
    return RequirementCoverageRead.model_validate(coverage, from_attributes=True)


@router.get("/{brief_id}/coverage", response_model=list[RequirementCoverageRead])
async def list_requirement_coverage(
    brief_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> list[RequirementCoverageRead]:
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.id == brief_id))
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product Brief {brief_id} not found"
        )
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    rows = (
        await db.scalars(
            select(RequirementCoverage)
            .where(RequirementCoverage.brief_id == brief_id)
            .order_by(RequirementCoverage.requirement_id)
        )
    ).all()
    return [RequirementCoverageRead.model_validate(row, from_attributes=True) for row in rows]


# --- the one admission step ----------------------------------------------------


@router.post("/{brief_id}/admit", response_model=ProductBriefAdmissionRead)
async def admit_product_brief_coverage(
    brief_id: str,
    body: ProductBriefPlanningAttemptCommand,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefAdmissionRead:
    """Cross the coverage-to-dispatch boundary once, in one transaction.

    Durable and idempotent. The already-admitted answer is given before the
    attempt is checked, because a second call is a *replay*, not a rival: the
    first call closed the attempt it presented, so demanding an active attempt
    would turn the retry of a succeeded call into an error. Everything that
    changes state is behind the attempt check.
    """
    brief = await load_brief_for_update(brief_id, db)
    await _authorize(brief.project_id, x_telegram_id, db, internal, credentials)
    story_id = _require_planning_subject(brief)
    if brief.coverage_admitted_at is not None:
        return ProductBriefAdmissionRead(
            brief_id=brief.id,
            story_id=story_id,
            outcome=ProductBriefAdmissionOutcome.ALREADY_ADMITTED,
            coverage_admitted_at=brief.coverage_admitted_at,
        )
    require_active_attempt(brief, body.planning_attempt_id)

    # Only this attempt's dispositions count. A plan taken over from a stale
    # architect starts from nothing covered, because its coverage rows point at
    # tasks that attempt will never release.
    dispositions = list(
        (
            await db.scalars(
                select(RequirementCoverage).where(
                    RequirementCoverage.brief_id == brief.id,
                    RequirementCoverage.planning_attempt_id == brief.planning_attempt_id,
                )
            )
        ).all()
    )
    covered = {row.requirement_id for row in dispositions}
    missing = sorted(_must_requirement_ids(brief) - covered)
    if missing:
        return ProductBriefAdmissionRead(
            brief_id=brief.id,
            story_id=story_id,
            outcome=ProductBriefAdmissionOutcome.INCOMPLETE,
            missing_requirement_ids=missing,
        )

    # Ascending task id, the order the dispatch admission point's rung 1 uses,
    # so a release and a dispatch decision queue rather than deadlock.
    tasks = list(
        (
            await db.scalars(
                select(Task)
                .where(
                    Task.story_id == story_id,
                    Task.planning_attempt_id == brief.planning_attempt_id,
                )
                .order_by(Task.id)
                .with_for_update()
            )
        ).all()
    )
    # Fail closed. Every counted disposition must still name a member of this
    # plan; the task update route freezes plan membership while a task is
    # unadmitted, so this should be unreachable. It is here because the durable
    # record must never be written over an inconsistency: an admission that
    # stamped `coverage_admitted_at` while a covering task sat outside the
    # release set would strand that task for good — the attempt is closed by
    # then, and the one admission step can only replay `already_admitted`.
    by_id = {task.id: task for task in tasks}
    stale = sorted(
        {
            row.task_id
            for row in dispositions
            if row.task_id is not None
            and not (row.task_id in by_id and _task_is_in_plan(by_id[row.task_id], brief, story_id))
        }
    )
    if stale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Product Brief coverage names tasks that are no longer part of this plan: "
                + ", ".join(stale)
            ),
        )

    released = [task.id for task in tasks if not task.dispatch_admitted]
    for task in tasks:
        task.dispatch_admitted = True
    brief.coverage_admitted_at = datetime.now(UTC)
    # The plan is complete, so nobody owns an incomplete plan any more.
    brief.planning_attempt_active = False
    await db.commit()
    await db.refresh(brief)
    logger.info(
        "product_brief_admitted",
        brief_id=brief.id,
        story_id=story_id,
        released_task_ids=released,
    )
    return ProductBriefAdmissionRead(
        brief_id=brief.id,
        story_id=story_id,
        outcome=ProductBriefAdmissionOutcome.ADMITTED,
        coverage_admitted_at=brief.coverage_admitted_at,
        released_task_ids=released,
    )
