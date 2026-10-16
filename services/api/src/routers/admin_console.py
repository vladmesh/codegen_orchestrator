"""Admin console read models: request journeys, runtime topology, attention feed."""

from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Story, StoryLink

from .. import admin_console, admin_journey_repository, admin_live_log
from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..schemas.admin_console import (
    AttentionResponse,
    JourneyDetail,
    JourneyRepository,
    JourneySummary,
    LiveLogResponse,
    StoryLinkCreate,
    StoryLinkView,
    TopologyResponse,
)

router = APIRouter(
    prefix="/admin/v2", tags=["admin"], dependencies=[Depends(require_internal_or_admin)]
)


@router.get("/attention", response_model=AttentionResponse)
async def attention(
    since: datetime | None = Query(None),
    project_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> AttentionResponse:
    return await admin_console.build_attention(db, since=since, project_id=project_id)


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


@router.get("/journeys/{story_id}/live-log", response_model=LiveLogResponse)
async def journey_live_log(
    story_id: str, db: AsyncSession = Depends(get_async_session)
) -> LiveLogResponse:
    live_log = await admin_live_log.read_story_live_log(db, story_id)
    if live_log is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return live_log


@router.get("/journeys/{story_id}/repository", response_model=JourneyRepository)
async def journey_repository(
    story_id: str, db: AsyncSession = Depends(get_async_session)
) -> JourneyRepository:
    repository = await admin_journey_repository.read_story_repository(db, story_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return repository


@router.post(
    "/journeys/{story_id}/links",
    response_model=StoryLinkView,
    status_code=status.HTTP_201_CREATED,
)
async def add_journey_link(
    story_id: str, body: StoryLinkCreate, db: AsyncSession = Depends(get_async_session)
) -> StoryLinkView:
    """Link a pull request made outside the story's own runs, e.g. a proposed reusable module."""
    if await db.get(Story, story_id) is None:
        raise HTTPException(status_code=404, detail="Story not found")
    link = StoryLink(story_id=story_id, kind=body.kind.value, title=body.title, url=body.url)
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return admin_console.link_view(link)


@router.delete("/journeys/{story_id}/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_journey_link(
    story_id: str, link_id: int, db: AsyncSession = Depends(get_async_session)
) -> None:
    link = await db.scalar(
        select(StoryLink).where(StoryLink.id == link_id, StoryLink.story_id == story_id)
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    await db.delete(link)
    await db.commit()


@router.get("/workers/{worker_id}/live-log", response_model=LiveLogResponse)
async def worker_live_log(
    worker_id: str, db: AsyncSession = Depends(get_async_session)
) -> LiveLogResponse:
    return await admin_live_log.read_worker_live_log(db, worker_id)


@router.get("/topology", response_model=TopologyResponse)
async def topology(db: AsyncSession = Depends(get_async_session)) -> TopologyResponse:
    return await admin_console.build_topology(db)
