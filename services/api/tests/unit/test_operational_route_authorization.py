"""Operational routers admit only administrators and internal services."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.routing import APIRoute, iter_route_contexts
from httpx import ASGITransport, AsyncClient
import pytest

from shared.models import User
from src.database import get_async_session
from src.dependencies import create_lk_jwt, require_internal_or_admin
from src.main import app

OPERATIONAL_PATHS = {
    "/api/analytics/hourly",
    "/api/analytics/daily",
    "/api/analytics/known-users",
    "/api/agent-configs/",
    "/api/agent-configs/{config_id}",
    "/api/service-deployments/",
    "/api/service-deployments/{deployment_id}",
    "/api/debug/queues",
    "/api/debug/queues/{stream}/messages",
    "/api/debug/queues/{stream}/{group}/pending",
    "/api/debug/queues/{stream}/{group}/ack/{message_id:path}",
    "/api/debug/queues/{stream}/messages/{message_id:path}",
}


def _operational_routes() -> list[tuple[str, APIRoute]]:
    return [
        (context.path, context.original_route)
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and context.path in OPERATIONAL_PATHS
    ]


def test_every_operational_route_declares_the_internal_or_admin_guard():
    routes = _operational_routes()
    assert {path for path, _ in routes} == OPERATIONAL_PATHS
    for path, route in routes:
        assert any(dep.call is require_internal_or_admin for dep in route.dependant.dependencies), (
            f"{path} must require internal or administrator access"
        )


@pytest.fixture
def actor_session():
    ordinary = User(id=101, telegram_id=1001, username="ordinary", is_admin=False)
    admin = User(id=102, telegram_id=1002, username="admin", is_admin=True)
    users = {ordinary.id: ordinary, admin.id: admin}
    session = AsyncMock()

    async def execute(statement):
        params = statement.compile().params
        user = users.get(
            next((value for key, value in params.items() if key.startswith("id_")), None)
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        result.scalars.return_value.all.return_value = []
        result.scalars.return_value.unique.return_value.all.return_value = []
        return result

    session.execute = execute

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override
    yield ordinary, admin
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/analytics/hourly?project_id=00000000-0000-0000-0000-000000000001",
        "/api/agent-configs/",
        "/api/service-deployments/",
    ],
)
async def test_operational_reads_reject_an_ordinary_bearer_and_admit_admin_and_internal(
    actor_session, path
):
    ordinary, admin = actor_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ordinary_response = await client.get(
            path, headers={"Authorization": f"Bearer {create_lk_jwt(ordinary.id)}"}
        )
        admin_response = await client.get(
            path, headers={"Authorization": f"Bearer {create_lk_jwt(admin.id)}"}
        )
        internal_response = await client.get(path, headers={"X-Internal-Key": "test-internal-key"})

    assert ordinary_response.status_code == HTTPStatus.FORBIDDEN, ordinary_response.text
    assert admin_response.status_code == HTTPStatus.OK, admin_response.text
    assert internal_response.status_code == HTTPStatus.OK, internal_response.text


@pytest.mark.asyncio
async def test_debug_queue_read_rejects_an_ordinary_bearer_and_admits_admin_and_internal(
    actor_session,
):
    ordinary, admin = actor_session
    redis = AsyncMock()
    redis.xinfo_stream = AsyncMock(return_value={"length": 0})
    redis.xinfo_groups = AsyncMock(return_value=[])
    redis.aclose = AsyncMock()

    with patch("src.routers.debug.aioredis.from_url", return_value=redis):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            ordinary_response = await client.get(
                "/api/debug/queues",
                headers={"Authorization": f"Bearer {create_lk_jwt(ordinary.id)}"},
            )
            admin_response = await client.get(
                "/api/debug/queues",
                headers={"Authorization": f"Bearer {create_lk_jwt(admin.id)}"},
            )
            internal_response = await client.get(
                "/api/debug/queues", headers={"X-Internal-Key": "test-internal-key"}
            )

    assert ordinary_response.status_code == HTTPStatus.FORBIDDEN, ordinary_response.text
    assert admin_response.status_code == HTTPStatus.OK, admin_response.text
    assert internal_response.status_code == HTTPStatus.OK, internal_response.text
