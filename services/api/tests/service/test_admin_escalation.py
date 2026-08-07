"""`is_admin` is not something a caller can ask for.

The escalation this closes, against a real database: anything that can reach the
API's port — a worker container, which shares a network with it — used to
`POST /api/users` with `is_admin: true` and then act as that administrator by
sending its own `X-Telegram-ID`. The route the platform actually needs, the bot
registering the accounts named in `ADMIN_TELEGRAM_IDS`, has to keep working, so
the flag is refused by who is asking rather than removed.
"""

from collections.abc import AsyncGenerator
from http import HTTPStatus
import os

from httpx import ASGITransport, AsyncClient
import pytest

ADMIN_BY_THE_BOT = 100_115_201
CLAIMED_BY_A_STRANGER = 100_115_202


@pytest.fixture
async def anonymous_client() -> AsyncGenerator[AsyncClient, None]:
    """A worker container: it can reach the port and nothing else."""
    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def internal_client() -> AsyncGenerator[AsyncClient, None]:
    """The bot: it holds the internal key, so it may say who is an admin."""
    from src.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]},
    ) as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/users/", "/api/users/upsert"])
async def test_an_outside_caller_cannot_write_itself_an_admin(
    anonymous_client, internal_client, path
):
    created = await anonymous_client.post(
        path,
        json={"telegram_id": CLAIMED_BY_A_STRANGER, "username": "stranger", "is_admin": True},
    )

    assert created.status_code == HTTPStatus.UNAUTHORIZED, created.text

    # And nothing was written: the user does not exist at all.
    lookup = await internal_client.get(f"/api/users/by-telegram/{CLAIMED_BY_A_STRANGER}")
    assert lookup.status_code == HTTPStatus.NOT_FOUND, lookup.text


@pytest.mark.asyncio
async def test_the_bot_still_registers_the_administrators_it_is_configured_with(internal_client):
    """The path that must survive: ADMIN_TELEGRAM_IDS reaches the database."""
    created = await internal_client.post(
        "/api/users/",
        json={"telegram_id": ADMIN_BY_THE_BOT, "username": "configured_admin", "is_admin": True},
    )
    assert created.status_code in (HTTPStatus.CREATED, HTTPStatus.BAD_REQUEST), created.text

    upserted = await internal_client.post(
        "/api/users/upsert",
        json={"telegram_id": ADMIN_BY_THE_BOT, "username": "configured_admin", "is_admin": True},
    )
    assert upserted.status_code == HTTPStatus.OK, upserted.text
    assert upserted.json()["is_admin"] is True

    stored = await internal_client.get(f"/api/users/by-telegram/{ADMIN_BY_THE_BOT}")
    assert stored.json()["is_admin"] is True, stored.text


@pytest.mark.asyncio
async def test_a_named_telegram_id_is_not_a_credential(anonymous_client):
    """The other half of the escalation: the header names a user, it proves none."""
    resp = await anonymous_client.get(
        "/api/projects/", headers={"X-Telegram-ID": str(ADMIN_BY_THE_BOT)}
    )

    assert resp.status_code == HTTPStatus.UNAUTHORIZED, resp.text
