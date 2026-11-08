"""The bot keeps reply recovery running while system config is unavailable."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.config_store import ConfigStoreUnavailableError
from src import main


@pytest.mark.asyncio
async def test_config_refresh_failure_preserves_recovery_and_closes_store():
    app = MagicMock(bot=AsyncMock())
    settings = MagicMock(redis_url="redis://fixture", api_base_url="http://fixture")
    settings.get_admin_ids.return_value = set()
    stream_client = MagicMock(connect=AsyncMock(), close=AsyncMock(), redis=AsyncMock())
    store = MagicMock()
    store.refresh = AsyncMock(side_effect=ConfigStoreUnavailableError("unavailable"))
    store.close = AsyncMock()

    async def refresh_forever():
        await asyncio.Event().wait()

    store.refresh_forever = refresh_forever

    async def listener_task(*_args):
        await asyncio.Event().wait()

    with (
        patch.object(main, "get_settings", return_value=settings),
        patch.object(main, "RedisStreamClient", return_value=stream_client),
        patch.object(main, "verify_po_storage", new=AsyncMock()),
        patch.object(main, "ConfigStore", return_value=store),
        patch.object(main, "ProvisionerNotifier") as notifier,
        patch.object(main, "ProactiveListener") as proactive,
        patch.object(main, "POResponseListener") as response_listener,
        patch.object(main.api_client, "close", new=AsyncMock()),
    ):
        notifier.return_value.start = AsyncMock(
            side_effect=lambda *_: asyncio.create_task(listener_task())
        )
        proactive.return_value.start = AsyncMock(
            side_effect=lambda *_: asyncio.create_task(listener_task())
        )
        response_listener.return_value.run = listener_task
        await main.post_init(app)
        assert main._po_response_listener_task is not None
        assert not main._po_response_listener_task.done()
        await main.post_shutdown(app)

    store.close.assert_awaited_once()
    stream_client.close.assert_awaited_once()
