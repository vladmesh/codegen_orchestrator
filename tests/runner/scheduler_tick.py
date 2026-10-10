"""One tick of the scheduler's own loop body, against the runner's API and Redis.

Run by `tests/runner/native_lifecycle.py` in the scheduler's environment
(`PYTHONPATH=services/scheduler:.`). `scaffolds` is `trigger_scaffolds`, the body of the
scaffold loop; `installs` is `dispatch_todo_tasks`, the body of the dispatcher loop. Nothing
is selected or published here: the output lists the scaffold-queue entries the tick itself
appended, read back from the stream by id.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from shared.queues import SCAFFOLD_QUEUE
from shared.redis import RedisStreamClient
from src import startup
from src.clients.api import SchedulerAPIClient
from src.tasks.scaffold_trigger import trigger_scaffolds
from src.tasks.task_dispatcher import dispatch_todo_tasks

#: The values the two loop bodies read; the API seeded them from the production configs.
CONFIG_KEYS = {
    "scheduler.scaffold_inflight_ttl",
    "scheduler.service_template_source",
    "scheduler.service_template_ref",
}


async def tick(step: str) -> dict:
    startup.init_config(CONFIG_KEYS)
    api = SchedulerAPIClient()
    stream = RedisStreamClient()
    await stream.connect()
    try:
        last = await stream.redis.xrevrange(SCAFFOLD_QUEUE, count=1)
        after = last[0][0] if last else "0-0"
        if step == "scaffolds":
            count = await trigger_scaffolds(api, stream)
        else:
            count = await dispatch_todo_tasks(api, stream)
        entries = await stream.redis.xrange(SCAFFOLD_QUEUE, min=f"({after}")
        return {
            "step": step,
            "count": count,
            "published": [
                {"entry_id": entry_id, "message": json.loads(fields["data"])}
                for entry_id, fields in entries
            ],
        }
    finally:
        await api.close()
        await stream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", choices=("scaffolds", "installs"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(asyncio.run(tick(args.step)), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
