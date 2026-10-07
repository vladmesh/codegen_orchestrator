"""Admin console read models: request journeys, runtime topology, attention feed."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .. import admin_console
from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..schemas.admin_console import (
    AttentionResponse,
    JourneyDetail,
    JourneySummary,
    TopologyResponse,
)

router = APIRouter(
    prefix="/admin/v2", tags=["admin"], dependencies=[Depends(require_internal_or_admin)]
)


@router.get("/attention", response_model=AttentionResponse)
async def attention(db: AsyncSession = Depends(get_async_session)) -> AttentionResponse:
    return await admin_console.build_attention(db)


@router.get("/journeys", response_model=list[JourneySummary])
async def journeys(
    limit: int = Query(admin_console.JOURNEY_LIST_LIMIT, ge=1, le=200),
    project_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> list[JourneySummary]:
    return await admin_console.list_journeys(db, limit, project_id)


@router.get("/journeys/{story_id}", response_model=JourneyDetail)
async def journey(story_id: str, db: AsyncSession = Depends(get_async_session)) -> JourneyDetail:
    detail = await admin_console.load_journey(db, story_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return detail


@router.get("/topology", response_model=TopologyResponse)
async def topology(db: AsyncSession = Depends(get_async_session)) -> TopologyResponse:
    return await admin_console.build_topology(db)
