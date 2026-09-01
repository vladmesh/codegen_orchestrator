"""Product Brief confirmation and architect coverage boundary."""

from datetime import UTC, datetime
from hashlib import sha256
import json
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.product_brief import (
    ProductBriefAdmissionOutcome,
    ProductBriefAdmissionRead,
    ProductBriefConfirm,
    ProductBriefCreate,
    ProductBriefRead,
    RequirementCoverageCreate,
    RequirementCoverageRead,
)
from shared.models import ProductBrief, RequirementCoverage, Task

from ..database import get_async_session
from ..dependencies import _optional_bearer_scheme, is_internal_service
from .projects_guards import check_project_access, load_locked_project

router = APIRouter(prefix="/product-briefs", tags=["product-briefs"])


def _content_dump(content: object) -> dict:
    return content.model_dump(mode="json")  # type: ignore[union-attr]


def _content_hash(content: dict) -> str:
    return sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _access(
    project_id: uuid.UUID,
    telegram_id: int | None,
    db: AsyncSession,
    internal: bool,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    project = await load_locked_project(db, project_id)
    await check_project_access(
        project, telegram_id, db, is_internal=internal, credentials=credentials
    )


@router.post("/", response_model=ProductBriefRead, status_code=status.HTTP_201_CREATED)
async def create_product_brief(
    body: ProductBriefCreate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefRead:
    """Create a new unconfirmed revision. Confirmed content is never updated."""
    await _access(body.project_id, x_telegram_id, db, internal, credentials)
    content = _content_dump(body.content)
    existing = (
        await db.execute(select(ProductBrief).where(ProductBrief.request_id == body.request_id))
    ).scalar_one_or_none()
    if existing:
        if (
            existing.project_id != body.project_id
            or existing.title != body.title
            or _content_hash(existing.content) != _content_hash(content)
        ):
            raise HTTPException(status_code=409, detail="product brief request content mismatch")
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
    await db.commit()
    await db.refresh(brief)
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
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.id == brief_id).with_for_update())
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(status_code=404, detail="Product Brief not found")
    await _access(brief.project_id, x_telegram_id, db, internal, credentials)
    if _content_hash(brief.content) != _content_hash(_content_dump(body.content)):
        raise HTTPException(
            status_code=409, detail="confirmation content does not match presented brief"
        )
    if brief.confirmed_at is not None:
        if brief.confirmation_request_id != body.request_id:
            raise HTTPException(status_code=409, detail="Product Brief is already confirmed")
        return ProductBriefRead.model_validate(brief, from_attributes=True)
    duplicate = (
        await db.execute(
            select(ProductBrief).where(ProductBrief.confirmation_request_id == body.request_id)
        )
    ).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=409, detail="confirmation request belongs to another brief")
    brief.confirmed_at = datetime.now(UTC)
    brief.confirmation_request_id = body.request_id
    await db.commit()
    await db.refresh(brief)
    return ProductBriefRead.model_validate(brief, from_attributes=True)


@router.get("/{brief_id}", response_model=ProductBriefRead)
async def get_product_brief(
    brief_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefRead:
    brief = await db.get(ProductBrief, brief_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="Product Brief not found")
    await _access(brief.project_id, x_telegram_id, db, internal, credentials)
    return ProductBriefRead.model_validate(brief, from_attributes=True)


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
    if body.requirement_id != requirement_id:
        raise HTTPException(status_code=422, detail="requirement ID path/body mismatch")
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.id == brief_id).with_for_update())
    ).scalar_one_or_none()
    if brief is None or brief.confirmed_at is None:
        raise HTTPException(status_code=422, detail="coverage requires a confirmed Product Brief")
    await _access(brief.project_id, x_telegram_id, db, internal, credentials)
    if brief.coverage_admitted_at is not None:
        raise HTTPException(status_code=409, detail="Product Brief coverage is already admitted")
    if requirement_id not in {item["id"] for item in brief.content["must_requirements"]}:
        raise HTTPException(status_code=422, detail="unknown Product Brief requirement")
    if body.task_id is not None:
        task = await db.get(Task, body.task_id)
        if task is None or task.project_id != brief.project_id or task.story_id != brief.story_id:
            raise HTTPException(
                status_code=422,
                detail="coverage task must belong to this Product Brief Story",
            )
    coverage = (
        await db.execute(
            select(RequirementCoverage).where(
                RequirementCoverage.brief_id == brief_id,
                RequirementCoverage.requirement_id == requirement_id,
            )
        )
    ).scalar_one_or_none()
    if coverage is None:
        coverage = RequirementCoverage(brief_id=brief_id, **body.model_dump())
        db.add(coverage)
    else:
        for key, value in body.model_dump().items():
            setattr(coverage, key, value)
    await db.commit()
    await db.refresh(coverage)
    return RequirementCoverageRead.model_validate(coverage, from_attributes=True)


@router.post("/{brief_id}/admit", response_model=ProductBriefAdmissionRead)
async def admit_product_brief_coverage(
    brief_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProductBriefAdmissionRead:
    """Atomically release planned tasks after every must-requirement is disposed."""
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.id == brief_id).with_for_update())
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(status_code=404, detail="Product Brief not found")
    await _access(brief.project_id, x_telegram_id, db, internal, credentials)
    if brief.confirmed_at is None or brief.story_id is None:
        raise HTTPException(
            status_code=422, detail="admission requires a confirmed Product Brief Story"
        )

    required = {item["id"] for item in brief.content["must_requirements"]}
    covered = set(
        (
            await db.execute(
                select(RequirementCoverage.requirement_id)
                .where(RequirementCoverage.brief_id == brief.id)
                .with_for_update()
            )
        ).scalars()
    )
    missing = sorted(required - covered)
    if missing:
        return ProductBriefAdmissionRead(
            brief_id=brief.id,
            story_id=brief.story_id,
            outcome=ProductBriefAdmissionOutcome.INCOMPLETE,
            missing_requirement_ids=missing,
        )
    if brief.coverage_admitted_at is not None:
        return ProductBriefAdmissionRead(
            brief_id=brief.id,
            story_id=brief.story_id,
            outcome=ProductBriefAdmissionOutcome.ALREADY_ADMITTED,
        )

    tasks = list(
        (
            await db.execute(select(Task).where(Task.story_id == brief.story_id).with_for_update())
        ).scalars()
    )
    released_task_ids = [task.id for task in tasks if not task.dispatch_admitted]
    for task in tasks:
        task.dispatch_admitted = True
    brief.coverage_admitted_at = datetime.now(UTC)
    await db.commit()
    return ProductBriefAdmissionRead(
        brief_id=brief.id,
        story_id=brief.story_id,
        outcome=ProductBriefAdmissionOutcome.ADMITTED,
        released_task_ids=released_task_ids,
    )


async def require_complete_product_brief_coverage(story_id: str, db: AsyncSession) -> None:
    """Fail closed before a new brief-backed product story becomes runnable."""
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.story_id == story_id))
    ).scalar_one_or_none()
    if brief is None:
        return  # Pre-brief stories preserve their historical lifecycle.
    if brief.confirmed_at is None:
        raise HTTPException(status_code=422, detail="Product Brief is not confirmed")
    required = {item["id"] for item in brief.content["must_requirements"]}
    covered = set(
        (
            await db.execute(
                select(RequirementCoverage.requirement_id).where(
                    RequirementCoverage.brief_id == brief.id
                )
            )
        ).scalars()
    )
    if missing := sorted(required - covered):
        raise HTTPException(status_code=422, detail={"missing_product_brief_coverage": missing})


async def require_product_brief_dispatch_admission(story_id: str, db: AsyncSession) -> None:
    """Reject lifecycle progression until the separate admission is durable."""
    await require_complete_product_brief_coverage(story_id, db)
    brief = (
        await db.execute(select(ProductBrief).where(ProductBrief.story_id == story_id))
    ).scalar_one_or_none()
    if brief is not None and brief.coverage_admitted_at is None:
        raise HTTPException(
            status_code=422,
            detail={"product_brief_admission_required": brief.id},
        )
