"""One scaffold tick and one dispatch tick of the native scheduler, for this test's rows only."""

import asyncio
import json
import os
from pathlib import Path

from shared.queues import SCAFFOLD_QUEUE
from shared.redis import RedisStreamClient
from src import startup
from src.clients.api import SchedulerAPIClient
from src.tasks.scaffold_trigger import trigger_scaffolds
from src.tasks.task_dispatcher import dispatch_todo_tasks


async def run():
    api = SchedulerAPIClient()
    stream = RedisStreamClient()
    await stream.connect()
    startup.init_config(
        {
            "scheduler.scaffold_inflight_ttl",
            "scheduler.service_template_source",
            "scheduler.service_template_ref",
        }
    )
    project = os.environ["DRAFT_PROJECT"]
    tasks = set(os.environ["DRAFT_TASKS"].split(","))
    projects, read = api.get_projects, api.get_tasks_by_status

    async def mine():
        return [item for item in await projects() if str(item.id) == project]

    async def selected(status):
        return [task for task in await read(status) if task.id in tasks]

    api.get_projects, api.get_tasks_by_status = mine, selected
    try:
        last = await stream.redis.xrevrange(SCAFFOLD_QUEUE, count=1)
        after = last[0][0] if last else "0-0"
        scaffolds = await trigger_scaffolds(api, stream)
        dispatched = await dispatch_todo_tasks(api, stream)
        entries = await stream.redis.xrange(SCAFFOLD_QUEUE, min=f"({after}")
        # Service loggers write to stdout; the result goes to its own file.
        Path(os.environ["RESULT_FILE"]).write_text(
            json.dumps(
                {
                    "scaffolds": scaffolds,
                    "dispatched": dispatched,
                    "published": [
                        {"entry_id": entry_id, "message": json.loads(fields["data"])}
                        for entry_id, fields in entries
                    ],
                }
            )
        )
    finally:
        await api.close()
        await stream.close()


asyncio.run(run())
