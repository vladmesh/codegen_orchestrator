"""Run refusal routes retain their wire responses without deprecated status warnings."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from wake_doubles import RunSession

from shared.contracts.dto.run import RunStatus, RunType
from shared.models import Run
from src.routers import runs

pytestmark = pytest.mark.filterwarnings("error::DeprecationWarning")


@pytest.fixture
def refusal_api():
    app = FastAPI()
    app.include_router(runs.router, prefix="/api")
    session = RunSession([], Run(id="refused-run", status=RunStatus.RUNNING, run_metadata={}))
    redis = AsyncMock()
    app.dependency_overrides[runs.get_async_session] = lambda: session
    app.dependency_overrides[runs.get_redis_client] = lambda: redis
    app.dependency_overrides[runs.is_internal_service] = lambda: True
    app.dependency_overrides[runs.require_internal_or_admin] = lambda: True
    return app, session, redis


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor",
    [{"after_created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat()}, {"after_id": "run-1"}],
    ids=["timestamp-only", "id-only"],
)
async def test_incomplete_grant_cursor_refuses_without_query(refusal_api, cursor):
    app, session, redis = refusal_api
    session.execute = AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/runs/qa-ssh-grants/held", params=cursor)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "after_created_at and after_id name one cursor and must be given together"
    }
    session.execute.assert_not_awaited()
    assert session.journal == []
    redis.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_type", "payload", "detail"),
    [
        (
            RunType.QA,
            {"status": "failed", "engineering_attempt": {}},
            "engineering_attempt is only valid for engineering runs",
        ),
        (
            RunType.ENGINEERING,
            {"status": "running", "engineering_attempt": {}},
            "engineering_attempt is only valid with a terminal engineering status",
        ),
        (
            RunType.ENGINEERING,
            {"status": "failed", "qa_accounting": {"executor_started": False}},
            "qa_accounting is only valid with a terminal QA status",
        ),
        (
            RunType.QA,
            {"status": "running", "qa_accounting": {"executor_started": False}},
            "qa_accounting is only valid with a terminal QA status",
        ),
    ],
    ids=["engineering-wrong-type", "engineering-nonterminal", "qa-wrong-type", "qa-nonterminal"],
)
async def test_invalid_accounting_refuses_without_settlement(
    refusal_api, run_type, payload, detail
):
    app, session, redis = refusal_api
    session.run.type = run_type
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch("/api/runs/refused-run", json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert session.run.status == RunStatus.RUNNING
    assert session.journal == []
    redis.publish.assert_not_awaited()
