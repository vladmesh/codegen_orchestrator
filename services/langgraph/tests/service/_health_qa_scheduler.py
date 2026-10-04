"""Native persisted QA handoff and verdict routing in the existing CI service stack."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import structlog

from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.redis import RedisStreamClient
from src import startup
from src.clients.api import SchedulerAPIClient
from src.tasks.supervisor.handoff import _execute_qa_handoff
from src.tasks.supervisor.qa import supervise_testing_stories


async def run():
    api = SchedulerAPIClient()
    stream = RedisStreamClient()
    await stream.connect()
    run_id = os.environ["HEALTH_QA_RUN"]
    mode = os.environ["HEALTH_QA_MODE"]
    try:
        run = await api.get_run(run_id)
        if mode == "publish":
            plan = QAHandoffPlan.model_validate(run.run_metadata[QA_HANDOFF_KEY])
            await _execute_qa_handoff(api, stream, run.id, plan, structlog.get_logger())
        elif mode == "route":
            startup.init_config(
                {"supervisor.qa_failure_max_fingerprint_attempts", "supervisor.qa_max_fix_attempts"}
            )
            read_stories = api.get_stories_by_status

            async def selected(status):
                return [story for story in await read_stories(status) if story.id == run.story_id]

            api.get_stories_by_status = selected
            # Only administrator Telegram delivery is controlled. API, routing,
            # owner notice persistence and Redis publication remain native.
            with patch("shared.notifications.send_telegram_message", AsyncMock(return_value=True)):
                await supervise_testing_stories(api, stream)
                again = await supervise_testing_stories(api, stream)
                assert all(count == 0 for count in again.values())
        else:
            raise AssertionError(mode)
    finally:
        await api.close()
        await stream.close()


asyncio.run(run())
