"""Capability previews: the Architect's answer, stored before a brief relies on it.

A preview is created only by an internal caller — the PO tool, after the Architect's
deterministic Python resolved the requests against the activated catalog snapshot. A
user can read its product projection; nobody reads its technical half through this
router. Product Brief creation and confirmation resolve a brief's capabilities against
the stored preview here (`plan_for_content`), so the plan beside a revision is derived
from what the server stored, never from what a caller sent.
"""

from datetime import UTC, datetime
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.capability_preview import (
    CapabilityPlan,
    CapabilityPlanRefusedError,
    CapabilityPreviewCreate,
    CapabilityPreviewProjection,
    CapabilityPreviewRead,
    CapabilityPreviewTechnical,
    CapabilityRefusal,
    CapabilityRefusalCode,
    derive_capability_plan,
)
from shared.contracts.dto.product_brief import ProductBriefContent
from shared.models import CapabilityPreview, Project

from ..database import get_async_session
from ..dependencies import _optional_bearer_scheme, is_internal_service, require_service_actor
from .projects_guards import check_project_access

logger = structlog.get_logger()

router = APIRouter(prefix="/capability-previews", tags=["capability-previews"])


def _read(preview: CapabilityPreview) -> CapabilityPreviewRead:
    return CapabilityPreviewRead(
        preview_id=preview.id,
        project_id=preview.project_id,
        created_at=preview.created_at,
        **CapabilityPreviewProjection.model_validate(preview.product).model_dump(),
    )


def capability_refusal(
    code: CapabilityRefusalCode, status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"capability_refusal": CapabilityRefusal(code=code).model_dump(mode="json")},
    )


async def plan_for_content(
    project_id: uuid.UUID,
    content: ProductBriefContent,
    db: AsyncSession,
    *,
    status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
) -> tuple[str, CapabilityPlan]:
    """The stored preview a brief names, and the plan its capabilities resolve to.

    Refuses with a product-safe `capability_refusal` detail — codes and request or
    question ids only — when the preview is unknown, belongs to another project, was
    made under another activated snapshot, or the brief's capabilities and answers do
    not resolve under it.
    """
    capabilities = content.capabilities
    if capabilities is None:
        raise RuntimeError("plan_for_content needs a capability-backed brief")
    preview = await db.get(CapabilityPreview, capabilities.preview_id)
    if preview is None:
        raise capability_refusal(CapabilityRefusalCode.PREVIEW_UNKNOWN, status_code)
    if preview.project_id != project_id:
        raise capability_refusal(CapabilityRefusalCode.PREVIEW_FOREIGN, status_code)
    try:
        plan = derive_capability_plan(
            preview_id=preview.id,
            product=CapabilityPreviewProjection.model_validate(preview.product),
            technical=CapabilityPreviewTechnical.model_validate(preview.technical),
            capabilities=capabilities,
            must_requirement_ids={requirement.id for requirement in content.must_requirements},
            initial_setting_keys={setting.key for setting in content.initial_settings},
            activation=CATALOG_ACTIVATION,
        )
    except CapabilityPlanRefusedError as refused:
        raise HTTPException(
            status_code=status_code,
            detail={"capability_refusal": refused.refusal.model_dump(mode="json")},
        ) from refused
    return preview.id, plan


async def _authorized_project(
    project_id: uuid.UUID,
    telegram_id: int | None,
    db: AsyncSession,
    internal: bool,
    credentials: HTTPAuthorizationCredentials | None,
) -> Project:
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    await check_project_access(
        project, telegram_id, db, is_internal=internal, credentials=credentials
    )
    return project


@router.post(
    "/",
    response_model=CapabilityPreviewRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_service_actor)],
)
async def create_capability_preview(
    body: CapabilityPreviewCreate,
    db: AsyncSession = Depends(get_async_session),
) -> CapabilityPreviewRead:
    """Store one preview. The platform only: a user cannot author a technical plan."""
    project = await db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if body.technical.activation != CATALOG_ACTIVATION:
        raise capability_refusal(CapabilityRefusalCode.PREVIEW_STALE)
    preview = CapabilityPreview(
        id=f"preview-{secrets.token_hex(12)}",
        project_id=body.project_id,
        created_at=datetime.now(UTC),
        requests=[request.model_dump(mode="json") for request in body.requests],
        product=body.product.model_dump(mode="json"),
        technical=body.technical.model_dump(mode="json", by_alias=True),
    )
    db.add(preview)
    await db.commit()
    await db.refresh(preview)
    logger.info(
        "capability_preview_created",
        preview_id=preview.id,
        project_id=str(preview.project_id),
        routes=[route.route.value for route in body.product.routes],
    )
    return _read(preview)


@router.get("/{preview_id}", response_model=CapabilityPreviewRead)
async def get_capability_preview(
    preview_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> CapabilityPreviewRead:
    """The product projection of a stored preview."""
    preview = await db.get(CapabilityPreview, preview_id)
    if preview is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Capability preview {preview_id} not found",
        )
    await _authorized_project(preview.project_id, x_telegram_id, db, internal, credentials)
    return _read(preview)
