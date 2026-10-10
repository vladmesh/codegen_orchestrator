"""A draft order through the real API, scheduler, Redis and scaffolder entrypoint.

The Architect's `create_install_task` plans the INSTALL on the draft; the native scheduler
publishes the full scaffold and leaves the INSTALL at admission (`workspace_not_ready`); the
scaffolder's own entrypoint fails that scaffold when GitHub refuses the repository. The
failure is bounded: the capability story stops, nothing is retried or dispatched, and an
ordinary planned story of the same draft keeps the path it always had.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import uuid

import pytest

from shared.queues import ENGINEERING_QUEUE, WORKER_COMMANDS
from src.agents.architect.tools import create_install_task
from src.catalog_install import INSTALL_PYTHON_VERSION, plan_install_payload
from src.clients.api import LanggraphAPIClient
from tests.unit.test_catalog_install import snapshot

HERE = Path(__file__).parent


def _service(script: str, **env: str) -> dict:
    ran = subprocess.run(
        [sys.executable, "-P", str(HERE / script)],
        env=os.environ | {"API_BASE_URL": os.environ["TEST_API_BASE_URL"], **env},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return json.loads(ran.stdout.strip().splitlines()[-1])


def _tick(project: str, tasks: list[str]) -> dict:
    return _service(
        "_draft_order_scheduler.py",
        PYTHONPATH="/app/scheduler:/app",
        DRAFT_PROJECT=project,
        DRAFT_TASKS=",".join(tasks),
    )


@pytest.mark.asyncio
async def test_a_draft_orders_failed_scaffold_stops_only_its_capability_story(real_redis, tmp_path):
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_API_BASE_URL"]
    telegram = uuid.uuid4().int % 1_000_000_000
    before = {queue: await real_redis.xlen(queue) for queue in (ENGINEERING_QUEUE, WORKER_COMMANDS)}
    try:
        await api.post("users/", json={"telegram_id": telegram, "username": "draft-owner"})
        project = await api.post(
            "projects/",
            headers={"X-Telegram-ID": str(telegram)},
            json={
                "title": "Fresh order",
                "status": "draft",
                "initiating_run_id": f"draft-{telegram}",
                "config": {"modules": ["backend", "tg_bot"]},
            },
        )
        pid = project["id"]
        await api.post(
            "repositories/",
            json={"project_id": pid, "name": "Fresh order", "git_url": f"pending://{pid}"},
        )
        capability = await api.post("stories/", json={"project_id": pid, "title": "Modules"})
        ordinary = await api.post("stories/", json={"project_id": pid, "title": "Notes"})
        for story in (capability, ordinary):
            await api.post(f"stories/{story['id']}/start")
        payload = plan_install_payload(snapshot(), "reminders", INSTALL_PYTHON_VERSION)
        with patch("src.agents.architect.tools.api_client", api):
            install = await create_install_task(
                payload, story_id=capability["id"], project_id=pid, planning_attempt_id=None
            )
        assert "error" not in install, install
        feature = await api.post(
            "tasks/",
            json={
                "project_id": pid,
                "story_id": ordinary["id"],
                "title": "Keep notes",
                "type": "feature",
                "status": "todo",
            },
        )
        tasks = [install["id"], feature["id"]]

        waiting = _tick(pid, tasks)

        [full] = [item for item in waiting["published"] if item["message"]["project_id"] == pid]
        assert full["message"]["mode"] == "full" and waiting["dispatched"] == 0
        assert not any(item["message"].get("task_id") for item in waiting["published"])
        project_row = await api.get(f"projects/{pid}")
        assert project_row["status"] == "draft"

        failed = _service(
            "_draft_order_scaffolder.py",
            PYTHONPATH="/app/scaffolder:/app",
            SCAFFOLD_ENTRY=full["entry_id"],
            WORKSPACE_BASE_PATH=str(tmp_path),
            GITHUB_ORG="ci",
        )

        assert failed["status"] == "failed"
        stopped = await api.get(f"stories/{capability['id']}")
        assert stopped["status"] == "failed"
        assert stopped["quarantine_reason"]["code"] == "scaffold_failed"
        kept = await api.get(f"stories/{ordinary['id']}")
        assert kept["status"] == "in_progress"
        project_row = await api.get(f"projects/{pid}")
        assert project_row["status"] == "draft" and project_row["config"]["scaffold_error"]
        install_row = await api.get(f"tasks/{install['id']}")
        assert install_row["status"] == "todo" and install_row["install_operation"] is None

        after = _tick(pid, tasks)

        assert after["published"] == [] and after["scaffolds"] == 0 and after["dispatched"] == 0
        assert before == {queue: await real_redis.xlen(queue) for queue in before}
    finally:
        await api.close()
