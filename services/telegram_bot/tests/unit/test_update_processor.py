"""Cancelling a same-user waiter must release admission without running its handler."""

import asyncio
from datetime import UTC, datetime
import gc
import warnings

from telegram import Chat, Message, Update, User

from src.update_processor import UserUpdateProcessor


async def test_cancelled_waiter_closes_handler_and_releases_user_ordering():
    processor = UserUpdateProcessor(2)
    # An actual user-bearing update is supplied by Application in production.
    update = Update(
        1,
        message=Message(
            1, datetime.now(UTC), Chat(1, "private"), from_user=User(1, "fixture", False)
        ),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    ran = []

    async def first():
        entered.set()
        await release.wait()
        ran.append("first")

    async def second():
        ran.append("second")

    with warnings.catch_warnings(record=True) as captured:
        first_task = asyncio.create_task(processor.process_update(update, first()))
        await entered.wait()
        second_task = asyncio.create_task(processor.process_update(update, second()))
        await asyncio.sleep(0)
        second_task.cancel()
        await asyncio.gather(second_task, return_exceptions=True)
        release.set()
        await first_task
        await processor.process_update(update, second())
        await processor.shutdown()
        gc.collect()
    assert ran == ["first", "second"]
    assert processor.current_concurrent_updates == 0
    assert not any(issubclass(w.category, RuntimeWarning) for w in captured)
