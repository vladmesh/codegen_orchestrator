"""Work items router — CRUD + action-based status transitions + events."""

from datetime import UTC, datetime
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.work_item import (
    VALID_TRANSITIONS,
    WorkItemEventType,
    WorkItemStatus,
)
from shared.models import WorkItem, WorkItemEvent

from ..database import get_async_session
from ..schemas.work_item import (
    WorkItemCreate,
    WorkItemEventCreate,
    WorkItemEventRead,
    WorkItemRead,
    WorkItemTransition,
    WorkItemUpdate,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/work-items", tags=["work-items"])


def _generate_id() -> str:
    return f"wi-{secrets.token_hex(4)}"


def _to_read(wi: WorkItem, last_event: str | None = None) -> WorkItemRead:
    elapsed = None
    if wi.created_at:
        elapsed = (datetime.now(UTC) - wi.created_at.replace(tzinfo=UTC)).total_seconds() / 60
    return WorkItemRead(
        id=wi.id,
        project_id=wi.project_id,
        type=wi.type,
        title=wi.title,
        description=wi.description,
        plan=wi.plan,
        status=wi.status,
        priority=wi.priority,
        acceptance_criteria=wi.acceptance_criteria,
        current_iteration=wi.current_iteration,
        max_iterations=wi.max_iterations,
        created_by=wi.created_by,
        source_brainstorm_id=getattr(wi, "source_brainstorm_id", None),
        milestone_id=getattr(wi, "milestone_id", None),
        created_at=wi.created_at,
        updated_at=wi.updated_at,
        last_event=last_event,
        elapsed_minutes=round(elapsed, 1) if elapsed is not None else None,
    )


async def _get_work_item(work_item_id: str, db: AsyncSession) -> WorkItem:
    query = select(WorkItem).where(WorkItem.id == work_item_id)
    result = await db.execute(query)
    wi = result.scalar_one_or_none()
    if not wi:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"WorkItem {work_item_id} not found",
        )
    return wi


async def _get_last_event_summary(work_item_id: str, db: AsyncSession) -> str | None:
    query = (
        select(WorkItemEvent)
        .where(WorkItemEvent.work_item_id == work_item_id)
        .order_by(WorkItemEvent.created_at.desc())
        .limit(1)
    )
    result = await db.execute(query)
    event = result.scalar_one_or_none()
    if not event:
        return None
    if event.event_type == WorkItemEventType.STATUS_CHANGE:
        return f"{event.from_status} → {event.to_status}"
    return f"{event.event_type}: {event.details}" if event.details else event.event_type


async def _create_status_event(
    wi: WorkItem,
    from_status: str,
    to_status: str,
    actor: str,
    details: dict,
    db: AsyncSession,
) -> WorkItemEvent:
    event = WorkItemEvent(
        work_item_id=wi.id,
        event_type=WorkItemEventType.STATUS_CHANGE,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        details=details,
    )
    db.add(event)
    return event


def _validate_transition(from_status: str, to_status: str) -> None:
    try:
        from_s = WorkItemStatus(from_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status: {from_status}",
        )
    try:
        to_s = WorkItemStatus(to_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status: {to_status}",
        )
    if to_s not in VALID_TRANSITIONS[from_s]:
        allowed = [s.value for s in VALID_TRANSITIONS[from_s]]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Cannot transition from {from_status} to {to_status}. " f"Allowed: {allowed}",
        )


# --- CRUD ---


@router.post("/", response_model=WorkItemRead, status_code=status.HTTP_201_CREATED)
async def create_work_item(
    body: WorkItemCreate,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    now = datetime.now(UTC)
    wi = WorkItem(
        id=_generate_id(),
        project_id=body.project_id,
        type=body.type.value,
        title=body.title,
        description=body.description,
        status=WorkItemStatus.BACKLOG.value,
        priority=body.priority,
        acceptance_criteria=body.acceptance_criteria,
        current_iteration=0,
        max_iterations=body.max_iterations,
        created_by=body.created_by,
        source_brainstorm_id=body.source_brainstorm_id,
        milestone_id=body.milestone_id,
        created_at=now,
        updated_at=now,
    )
    db.add(wi)
    await db.commit()
    await db.refresh(wi)

    logger.info("work_item_created", work_item_id=wi.id, title=wi.title, type=wi.type)
    return _to_read(wi)


@router.get("/", response_model=list[WorkItemRead])
async def list_work_items(
    project_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    type_filter: str | None = Query(None, alias="type"),
    milestone_id: str | None = Query(None),
    since: datetime | None = Query(None),
    limit: int | None = Query(None, ge=1),
    sort: str | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[WorkItemRead]:
    query = select(WorkItem)

    if project_id:
        query = query.where(WorkItem.project_id == project_id)
    if status_filter:
        query = query.where(WorkItem.status == status_filter)
    if type_filter:
        query = query.where(WorkItem.type == type_filter)
    if milestone_id:
        query = query.where(WorkItem.milestone_id == milestone_id)
    if since:
        query = query.where(WorkItem.updated_at >= since)

    # Sorting
    if sort == "-created_at":
        query = query.order_by(WorkItem.created_at.desc())
    elif sort == "created_at":
        query = query.order_by(WorkItem.created_at.asc())
    else:
        query = query.order_by(WorkItem.priority.asc(), WorkItem.created_at.asc())

    if limit is not None:
        query = query.limit(limit)

    result = await db.execute(query)
    items = result.scalars().all()
    return [_to_read(wi) for wi in items]


@router.get("/stats")
async def get_work_item_stats(
    project_id: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    """Return counts of work items by status."""
    counts = {}
    total = 0
    for s in WorkItemStatus:
        query = select(func.count()).select_from(WorkItem).where(WorkItem.status == s.value)
        if project_id:
            query = query.where(WorkItem.project_id == project_id)
        result = await db.execute(query)
        count = result.scalar_one()
        counts[s.value] = count
        total += count
    counts["total"] = total
    return counts


@router.get("/next-tag")
async def get_next_tag(
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    """Return the next available backlog tag number (max existing tag + 1)."""
    # Titles follow pattern "#N Title..."
    query = select(WorkItem.title).order_by(WorkItem.created_at.desc())
    result = await db.execute(query)
    titles = result.scalars().all()

    max_tag = 0
    for title in titles:
        match = re.match(r"^#(\d+)\s", title)
        if match:
            tag = int(match.group(1))
            max_tag = max(tag, max_tag)

    return {"next_tag": max_tag + 1}


@router.get("/by-tag/{tag}", response_model=WorkItemRead)
async def get_work_item_by_tag(
    tag: str,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    """Lookup work item by backlog tag (e.g. '53' finds item titled '#53 ...')."""
    query = select(WorkItem).where(WorkItem.title.like(f"#{tag} %"))
    result = await db.execute(query)
    wi = result.scalar_one_or_none()
    if not wi:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No work item with tag #{tag}",
        )
    last_event = await _get_last_event_summary(wi.id, db)
    return _to_read(wi, last_event=last_event)


@router.get("/{work_item_id}", response_model=WorkItemRead)
async def get_work_item(
    work_item_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    wi = await _get_work_item(work_item_id, db)
    last_event = await _get_last_event_summary(work_item_id, db)
    return _to_read(wi, last_event=last_event)


@router.patch("/{work_item_id}", response_model=WorkItemRead)
async def update_work_item(
    work_item_id: str,
    body: WorkItemUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    wi = await _get_work_item(work_item_id, db)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(wi, field, value)

    await db.commit()
    await db.refresh(wi)

    logger.info("work_item_updated", work_item_id=wi.id, fields=list(update_data.keys()))
    return _to_read(wi)


@router.delete("/{work_item_id}", response_model=WorkItemRead)
async def cancel_work_item(
    work_item_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    wi = await _get_work_item(work_item_id, db)
    if wi.status == WorkItemStatus.CANCELLED:
        return _to_read(wi)

    _validate_transition(wi.status, WorkItemStatus.CANCELLED)

    old_status = wi.status
    wi.status = WorkItemStatus.CANCELLED
    await _create_status_event(wi, old_status, WorkItemStatus.CANCELLED, "system", {}, db)
    await db.commit()
    await db.refresh(wi)

    logger.info("work_item_cancelled", work_item_id=wi.id)
    return _to_read(wi)


# --- Action endpoints (state machine transitions) ---


@router.post("/{work_item_id}/start", response_model=WorkItemRead)
async def start_work_item(
    work_item_id: str,
    body: WorkItemTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    body = body or WorkItemTransition()
    wi = await _get_work_item(work_item_id, db)

    # Allow start from backlog (auto-promote to todo first) or from todo
    if wi.status == WorkItemStatus.BACKLOG:
        await _create_status_event(
            wi, WorkItemStatus.BACKLOG, WorkItemStatus.TODO, body.actor, {}, db
        )
        wi.status = WorkItemStatus.TODO

    _validate_transition(wi.status, WorkItemStatus.IN_DEV)

    old_status = wi.status
    wi.status = WorkItemStatus.IN_DEV
    await _create_status_event(wi, old_status, WorkItemStatus.IN_DEV, body.actor, body.details, db)
    await db.commit()
    await db.refresh(wi)

    logger.info("work_item_started", work_item_id=wi.id)
    return _to_read(wi)


@router.post("/{work_item_id}/complete", response_model=WorkItemRead)
async def complete_work_item(
    work_item_id: str,
    body: WorkItemTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    body = body or WorkItemTransition()
    wi = await _get_work_item(work_item_id, db)

    _validate_transition(wi.status, WorkItemStatus.DONE)

    old_status = wi.status
    wi.status = WorkItemStatus.DONE
    await _create_status_event(wi, old_status, WorkItemStatus.DONE, body.actor, body.details, db)
    await db.commit()
    await db.refresh(wi)

    logger.info("work_item_completed", work_item_id=wi.id)
    return _to_read(wi)


@router.post("/{work_item_id}/fail", response_model=WorkItemRead)
async def fail_work_item(
    work_item_id: str,
    body: WorkItemTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    body = body or WorkItemTransition()
    wi = await _get_work_item(work_item_id, db)

    _validate_transition(wi.status, WorkItemStatus.FAILED)

    old_status = wi.status
    wi.status = WorkItemStatus.FAILED
    details = body.details.copy()
    if body.reason:
        details["reason"] = body.reason
    await _create_status_event(wi, old_status, WorkItemStatus.FAILED, body.actor, details, db)
    await db.commit()
    await db.refresh(wi)

    logger.info("work_item_failed", work_item_id=wi.id, reason=body.reason)
    return _to_read(wi)


@router.post("/{work_item_id}/reopen", response_model=WorkItemRead)
async def reopen_work_item(
    work_item_id: str,
    body: WorkItemTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    body = body or WorkItemTransition()
    wi = await _get_work_item(work_item_id, db)

    _validate_transition(wi.status, WorkItemStatus.BACKLOG)

    old_status = wi.status
    wi.status = WorkItemStatus.BACKLOG
    details = body.details.copy()
    if body.reason:
        details["reason"] = body.reason
    await _create_status_event(wi, old_status, WorkItemStatus.BACKLOG, body.actor, details, db)
    await db.commit()
    await db.refresh(wi)

    logger.info("work_item_reopened", work_item_id=wi.id, reason=body.reason)
    return _to_read(wi)


@router.post("/{work_item_id}/transition", response_model=WorkItemRead)
async def transition_work_item(
    work_item_id: str,
    to_status: str = Query(...),
    body: WorkItemTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemRead:
    body = body or WorkItemTransition()
    wi = await _get_work_item(work_item_id, db)

    _validate_transition(wi.status, to_status)

    old_status = wi.status
    wi.status = to_status
    await _create_status_event(wi, old_status, to_status, body.actor, body.details, db)
    await db.commit()
    await db.refresh(wi)

    logger.info("work_item_transitioned", work_item_id=wi.id, from_s=old_status, to_s=to_status)
    return _to_read(wi)


# --- Events ---


@router.get("/{work_item_id}/events", response_model=list[WorkItemEventRead])
async def list_work_item_events(
    work_item_id: str,
    event_type: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> list[WorkItemEventRead]:
    await _get_work_item(work_item_id, db)

    query = (
        select(WorkItemEvent)
        .where(WorkItemEvent.work_item_id == work_item_id)
        .order_by(WorkItemEvent.created_at.asc())
    )
    if event_type:
        query = query.where(WorkItemEvent.event_type == event_type)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.post(
    "/{work_item_id}/events",
    response_model=WorkItemEventRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_work_item_event(
    work_item_id: str,
    body: WorkItemEventCreate,
    db: AsyncSession = Depends(get_async_session),
) -> WorkItemEvent:
    await _get_work_item(work_item_id, db)

    now = datetime.now(UTC)
    event = WorkItemEvent(
        work_item_id=work_item_id,
        event_type=body.event_type,
        iteration=body.iteration,
        details=body.details,
        actor=body.actor,
        created_at=now,
        updated_at=now,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    logger.info(
        "work_item_event_created",
        work_item_id=work_item_id,
        event_type=event.event_type,
    )
    return event
