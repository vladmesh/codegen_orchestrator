"""Run the native scheduler dispatcher against the service API and Redis."""

import asyncio
import os

from shared.redis import RedisStreamClient
from src import startup
from src.clients.api import SchedulerAPIClient
from src.tasks.task_dispatcher import dispatch_todo_tasks


async def run():
    api = SchedulerAPIClient()
    stream = RedisStreamClient()
    await stream.connect()
    startup.init_config({"scheduler.service_template_source", "scheduler.service_template_ref"})
    read = api.get_tasks_by_status

    async def selected(status):
        return [task for task in await read(status) if task.id == os.environ["INSTALL_TASK"]]

    api.get_tasks_by_status = selected
    try:
        assert await dispatch_todo_tasks(api, stream) == 1
        assert await dispatch_todo_tasks(api, stream) == 0
    finally:
        await api.close()
        await stream.close()


asyncio.run(run())
