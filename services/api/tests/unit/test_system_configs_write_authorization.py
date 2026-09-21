"""System-config writes admit only internal services and administrators.

The hole this closes: the app-wide gate only asks that a caller be authenticated,
so any user holding an LK bearer could rewrite the scheduler and admission
constants the whole fleet runs on. Reads stay where they were.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

from fastapi.routing import APIRoute, iter_route_contexts
from httpx import ASGITransport, AsyncClient
import pytest

from shared.models import SystemConfig, User
from src.database import get_async_session
from src.dependencies import create_lk_jwt, require_internal_or_admin
from src.main import app

WRITE_ROUTES = {
    ("POST", "/api/system-configs/"),
    ("PATCH", "/api/system-configs/{key:path}"),
    ("DELETE", "/api/system-configs/{key:path}"),
}

CONFIG_KEY = "scheduler.dispatch_interval_seconds"
CREATE_BODY = {"key": CONFIG_KEY, "value": 30, "category": "scheduler"}


def test_every_system_config_write_declares_the_internal_or_admin_guard():
    found = set()
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods):
            if (method, context.path) not in WRITE_ROUTES:
                continue
            found.add((method, context.path))
            assert any(
                dep.call is require_internal_or_admin for dep in route.dependant.dependencies
            ), f"{method} {context.path} must require internal or administrator access"

    assert found == WRITE_ROUTES


@pytest.fixture
def actor_session():
    """Users for the guard to resolve, plus a stored config for the writes to hit."""
    ordinary = User(id=201, telegram_id=2001, username="ordinary", is_admin=False)
    admin = User(id=202, telegram_id=2002, username="admin", is_admin=True)
    users = {ordinary.telegram_id: ordinary, ordinary.id: ordinary, admin.id: admin}
    stored = SystemConfig(
        key=CONFIG_KEY,
        value=30,
        category="scheduler",
        description="Task dispatcher poll interval in seconds",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    session = AsyncMock()
    session.add = MagicMock()

    async def execute(statement):
        params = statement.compile().params
        user = users.get(
            next((value for key, value in params.items() if key.startswith("telegram_id")), None)
        ) or users.get(
            next((value for key, value in params.items() if key.startswith("id_")), None)
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        return result

    async def get(model, key):
        return stored if model is SystemConfig else None

    session.execute = execute
    session.get = get

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override
    yield ordinary, admin
    app.dependency_overrides.clear()


async def _writes(client: AsyncClient, headers: dict) -> list[int]:
    post = await client.post("/api/system-configs/", json=CREATE_BODY, headers=headers)
    patch = await client.patch(
        f"/api/system-configs/{CONFIG_KEY}", json={"value": 45}, headers=headers
    )
    delete = await client.delete(f"/api/system-configs/{CONFIG_KEY}", headers=headers)
    return [post.status_code, patch.status_code, delete.status_code]


@pytest.mark.asyncio
async def test_an_ordinary_lk_bearer_cannot_write_system_configs(actor_session):
    ordinary, _ = actor_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        statuses = await _writes(client, {"Authorization": f"Bearer {create_lk_jwt(ordinary.id)}"})

    assert statuses == [HTTPStatus.FORBIDDEN] * 3


@pytest.mark.asyncio
async def test_internal_key_and_admin_bearer_still_write_system_configs(actor_session):
    _, admin = actor_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        internal = await _writes(client, {"X-Internal-Key": "test-internal-key"})
        as_admin = await _writes(client, {"Authorization": f"Bearer {create_lk_jwt(admin.id)}"})

    expected = [HTTPStatus.CREATED, HTTPStatus.OK, HTTPStatus.NO_CONTENT]
    assert internal == expected
    assert as_admin == expected
