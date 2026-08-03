"""Service-specific API client for Telegram bot."""

from __future__ import annotations

from shared.clients.internal_api import InternalAPIClient
from src.config import get_settings

# A user is waiting on the other side of every one of these calls, so the bot
# gives the API a third of the time the background services do.
TELEGRAM_TIMEOUT_SECONDS = 10.0


class TelegramAPIClient(InternalAPIClient):
    """HTTP client for Telegram bot API usage."""

    def __init__(self) -> None:
        super().__init__(get_settings().api_base_url, timeout=TELEGRAM_TIMEOUT_SECONDS)

    async def get_json(self, path: str, headers: dict | None = None) -> dict | list:
        resp = await self.request("GET", path, headers=headers)
        return resp.json()

    async def post_json(self, path: str, headers: dict | None = None, **kwargs) -> dict:
        resp = await self.request("POST", path, headers=headers, **kwargs)
        return resp.json()


api_client = TelegramAPIClient()
