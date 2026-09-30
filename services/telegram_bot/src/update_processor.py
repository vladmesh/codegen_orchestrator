"""Bounded native PTB dispatch with ordering for each user's mutable context."""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from telegram import Update
from telegram.ext import BaseUpdateProcessor


@dataclass
class _UserUpdates:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class UserUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates: int):
        super().__init__(max_concurrent_updates)
        self._users: dict[int, _UserUpdates] = {}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        # Application.stop drains native processing tasks before shutdown.
        self._users.clear()

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        if not isinstance(update, Update) or update.effective_user is None:
            await coroutine
            return
        user_id = update.effective_user.id
        pending = self._users.setdefault(user_id, _UserUpdates())
        pending.users += 1
        started = False
        try:
            async with pending.lock:
                started = True
                await coroutine
        finally:
            if not started:
                # PTB passes a coroutine. Cancellation while waiting must close it.
                coroutine.close()
            pending.users -= 1
            if pending.users == 0:
                del self._users[user_id]
