"""An id listed in ADMIN_TELEGRAM_IDS is admitted as an admin, or not at all.

`test_promo_registration.py` covers the unknown-user side of `auth_middleware`.
This file covers the env-listed owner: the branch that grants access before the
database is consulted, and the registration failure that must still stop the
update rather than admit an unregistered admin.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from src import middleware

ADMIN_ID = 123456789


@pytest.fixture
def admin_whitelist(monkeypatch):
    settings = MagicMock()
    settings.get_admin_ids.return_value = {ADMIN_ID, 987654321}
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)
    return settings


@pytest.mark.asyncio
async def test_env_listed_admin_is_granted_access_without_a_database_lookup(
    admin_whitelist, monkeypatch
) -> None:
    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    context = MagicMock()
    context.user_data = {}

    db_lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(middleware, "_check_user_in_db", db_lookup)
    monkeypatch.setattr(middleware, "_upsert_user", AsyncMock(return_value=True))

    assert await middleware.auth_middleware(update, context) is True
    assert context.user_data[middleware.USER_IS_ADMIN_KEY] is True
    db_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_admin_whose_registration_fails_is_stopped(admin_whitelist, monkeypatch) -> None:
    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    context = MagicMock()
    context.user_data = {}

    monkeypatch.setattr(middleware, "_upsert_user", AsyncMock(return_value=False))

    with pytest.raises(ApplicationHandlerStop):
        await middleware.auth_middleware(update, context)
    assert middleware.USER_IS_ADMIN_KEY not in context.user_data
