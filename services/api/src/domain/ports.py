"""Domain helpers for allocating runtime ports."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import PortAllocation


class PortAllocationExhaustedError(RuntimeError):
    """No unique port could be reserved within the bounded retry budget."""


async def allocate_next_port(
    db: AsyncSession,
    *,
    server_handle: str,
    application_id: int,
    service_name: str,
    start_port: int = 8000,
    max_retries: int = 10,
) -> PortAllocation:
    """Reserve the next free port without rolling back the caller's transaction.

    A SELECT FOR UPDATE cannot lock a gap when a server has no allocation for the
    candidate port yet. The unique constraint is therefore the final arbiter. A
    nested transaction contains that expected conflict so callers that already
    staged an Application/Repository/Run do not lose their outer transaction.
    """
    for _attempt in range(max_retries):
        result = await db.execute(
            select(PortAllocation.port)
            .where(PortAllocation.server_handle == server_handle)
            .with_for_update()
        )
        allocated_ports = {row[0] for row in result.all()}

        port = start_port
        while port in allocated_ports:
            port += 1

        allocation = PortAllocation(
            server_handle=server_handle,
            port=port,
            service_name=service_name,
            application_id=application_id,
        )
        try:
            async with db.begin_nested():
                db.add(allocation)
                await db.flush()
        except IntegrityError:
            continue
        return allocation

    raise PortAllocationExhaustedError(
        f"failed to allocate a unique port for {server_handle} after {max_retries} attempts"
    )
