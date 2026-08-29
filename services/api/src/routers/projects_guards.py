"""The project-row lock and the canonical access check, in one place.

Every config writer takes the same row lock and asks the same "may this caller
reach this project" question. They lived as private functions of the projects
router, which meant a new module that needed them either duplicated them or
reached into the router's privates.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Project

from ..dependencies import resolve_actor


async def check_project_access(
    project: Project,
    telegram_id: int | None,
    db: AsyncSession,
    *,
    is_internal: bool = False,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    """Check if the request may reach this project. Raises 401/403/404 if denied.

    Who is acting is `resolve_actor`'s decision, not this function's: a service
    acting for itself passes, a named user is judged as that user however the
    request was authenticated.
    """
    actor = await resolve_actor(
        is_internal=is_internal,
        telegram_id=telegram_id,
        credentials=credentials,
        db=db,
    )

    if actor is None or actor.is_admin:
        return

    # Regular user: must be owner; unowned projects are admin-only
    if project.owner_id != actor.id:
        raise HTTPException(status_code=403, detail="Access denied: not project owner")


async def load_locked_project(db: AsyncSession, project_id: uuid.UUID) -> Project:
    """Load one project under the lock used by every config writer."""
    query = select(Project).where(Project.id == project_id).with_for_update()
    project = (await db.execute(query)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


__all__ = ["check_project_access", "load_locked_project"]
