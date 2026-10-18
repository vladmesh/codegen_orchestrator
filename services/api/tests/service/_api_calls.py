"""Real API requests and committed Run readbacks shared by the api service tests."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Run


async def call_api(client: AsyncClient, method: str, path: str, status: int = 200, **kwargs):
    """One API request answered with `status`, as its JSON body."""
    response = await client.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    return response.json()


async def committed_run_outcome(db_engine, run_id: str) -> tuple[str, dict | None]:
    """The Run's status and result as a fresh database session reads them."""
    async with AsyncSession(db_engine) as session:
        row = (await session.execute(select(Run.status, Run.result).where(Run.id == run_id))).one()
        return row.status, row.result
