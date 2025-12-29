"""Allocations router.

Provides endpoints for managing port allocations across servers.
Phase 4 addition for infrastructure capability.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import PortAllocation, User

from ..database import get_async_session
from ..schemas import PortAllocationRead

router = APIRouter(prefix="/allocations", tags=["allocations"])


async def _require_admin_if_user(
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """Require admin if X-Telegram-ID header is provided."""
    if x_telegram_id is None:
        return

    query = select(User).where(User.telegram_id == x_telegram_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with telegram_id {x_telegram_id} not found",
        )

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required for allocation management",
        )


@router.get("/", response_model=list[PortAllocationRead])
async def list_allocations(
    project_id: str | None = None,
    server_handle: str | None = None,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(_require_admin_if_user),
) -> list[PortAllocation]:
    """List port allocations with optional filtering."""
    query = select(PortAllocation)

    if project_id:
        query = query.where(PortAllocation.project_id == project_id)

    if server_handle:
        query = query.where(PortAllocation.server_handle == server_handle)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/{allocation_id}", response_model=PortAllocationRead)
async def get_allocation(
    allocation_id: int,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(_require_admin_if_user),
) -> PortAllocation:
    """Get a single allocation by ID."""
    allocation = await db.get(PortAllocation, allocation_id)
    if not allocation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Allocation {allocation_id} not found",
        )
    return allocation


@router.delete("/{allocation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_allocation(
    allocation_id: int,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(_require_admin_if_user),
) -> None:
    """Delete (release) a port allocation."""
    allocation = await db.get(PortAllocation, allocation_id)
    if not allocation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Allocation {allocation_id} not found",
        )

    await db.delete(allocation)
    await db.commit()
