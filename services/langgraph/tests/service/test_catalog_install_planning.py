"""No-model planning through the real API, coverage, scheduler and Redis boundaries."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, patch
import uuid

from psycopg import AsyncConnection
import pytest

from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import ENGINEERING_QUEUE, SCAFFOLD_QUEUE, WORKER_COMMANDS
from src.clients.api import LanggraphAPIClient
from src.scripted_install_plan import scripted_install_plan
from tests.unit.test_catalog_install import snapshot


@pytest.mark.asyncio
async def test_scripted_selection_persists_one_install_and_dispatches_without_engineering(
    real_redis,
):
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_API_BASE_URL"]
    telegram = uuid.uuid4().int % 1_000_000_000
    before = {queue: await real_redis.xlen(queue) for queue in (ENGINEERING_QUEUE, WORKER_COMMANDS)}
    try:
        await api.post("users/", json={"telegram_id": telegram, "username": "catalog-owner"})
        project = await api.post(
            "projects/",
            headers={"X-Telegram-ID": str(telegram)},
            json={
                "title": "Owned notes",
                "status": "active",
                "initiating_run_id": "catalog-init",
                "config": {"workspace_ready": True, "modules": ["backend", "tg_bot"]},
            },
        )
        pid = project["id"]
        repo = await api.post(
            "repositories/",
            json={
                "project_id": pid,
                "role": "primary",
                "name": f"notes-{telegram}",
                "git_url": f"https://github.com/synthetic/notes-{telegram}",
            },
        )
        story = await api.post(
            "stories/", json={"project_id": pid, "title": "Add catalog reminders"}
        )
        content = {
            "summary": "Add reminders to my notes bot",
            "language": "en",
            "must_requirements": [
                {
                    "id": "remind",
                    "text": "Confirm, send and cancel a one-time reminder",
                    "user_wording": "Add the catalog reminders capability",
                }
            ],
            "usage_examples": [
                {
                    "requirement_id": "remind",
                    "user_sends": "Remind me to call Sam tomorrow at 10",
                    "product_answers": "Confirm the reminder, then list or cancel it",
                }
            ],
        }
        brief = await api.post(
            "product-briefs/",
            json={
                "project_id": pid,
                "title": "Add reminders",
                "content": content,
                "request_id": f"request-{telegram}",
            },
        )
        await api.post(
            f"product-briefs/{brief['id']}/confirm",
            json={"request_id": f"confirm-{telegram}", "content": content},
        )
        await api.post(f"product-briefs/{brief['id']}/story", json={"story_id": story["id"]})
        reader = AsyncMock()
        reader.read.return_value = snapshot()
        # Only catalog transport is controlled. This resource corpus is copied
        # byte-for-byte from the released kit; closure and all persistence are real.
        with (
            patch("src.scripted_install_plan.api_client", api),
            patch("src.agents.architect.tools.api_client", api),
            patch("src.scripted_install_plan.get_kit_catalog_reader", return_value=reader),
            patch(
                "src.llm.openrouter.ChatOpenAI", side_effect=AssertionError("model is forbidden")
            ),
        ):
            planned = await scripted_install_plan(pid, story["id"], "reminders", ["remind"])
        assert "error" not in planned, planned
        assert planned["coverage_outcome"] == "admitted", planned
        tasks = await api.get_tasks_by_story(story["id"])
        assert len(tasks) == 1
        task = tasks[0]
        assert task.type == "install" and task.repository_id == repo["id"]
        assert task.install.package.version == "0.5.0" and task.dispatch_admitted is True
        coverage = await api.list_requirement_coverage(brief["id"])
        assert len(coverage) == 1 and coverage[0].task_id == task.id
        assert coverage[0].planning_attempt_id == task.planning_attempt_id
        dispatched = subprocess.run(
            [sys.executable, "-P", str(Path(__file__).with_name("_catalog_install_scheduler.py"))],
            env=os.environ
            | {
                "PYTHONPATH": "/app/scheduler:/app",
                "INSTALL_TASK": task.id,
                "API_BASE_URL": os.environ["TEST_API_BASE_URL"],
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert dispatched.returncode == 0, dispatched.stdout + dispatched.stderr
        messages = await real_redis.xrange(SCAFFOLD_QUEUE)
        installed = [
            ScaffoldMessage.model_validate_json(fields[b"data"])
            for _, fields in messages
            if json.loads(fields[b"data"]).get("task_id") == task.id
        ]
        assert len(installed) == 1
        assert installed[0].install == task.install and installed[0].mode == "install"
        assert installed[0].operation_id
        assert before == {queue: await real_redis.xlen(queue) for queue in before}
        async with await AsyncConnection.connect(
            os.environ["TEST_DATABASE_URL"], autocommit=True
        ) as db:
            for table in ("runs", "engineering_attempt_ledger", "engineering_budget_reservations"):
                async with db.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT count(*) FROM {table} WHERE project_id=%s", (pid,)
                    )
                    assert (await cursor.fetchone())[0] == 0
    finally:
        await api.close()
