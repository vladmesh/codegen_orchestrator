"""A product-local INSTALL from the native scheduler through Redis to the scaffolder's consumer.

The real API, PostgreSQL and Redis own admission, the claim, every fence and the settlement.
The scaffolder's GitHub token and its native executor are the controlled edges, declared in
`_local_install_scaffolder.py`; nothing here proves the kit's source installation itself.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from psycopg import AsyncConnection
import pytest

from shared.contracts.dto.local_install import LocalPackageInstall
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import ENGINEERING_QUEUE, SCAFFOLD_QUEUE, WORKER_COMMANDS
from src.clients.api import LanggraphAPIClient

HERE = Path(__file__).parent
HEAD = "9" * 40


def _service(script: str, **env: str) -> subprocess.CompletedProcess:
    ran = subprocess.run(
        [sys.executable, "-P", str(HERE / script)],
        env=os.environ | {"API_BASE_URL": os.environ["TEST_API_BASE_URL"], **env},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return ran


def _local_install(project: str, repository: str) -> dict:
    return {
        "route": "package_source",
        "project_id": project,
        "repository_id": repository,
        "commit": "1" * 40,
        "path": "packages/notes_digest",
        "package": {
            "name": "notes-digest",
            "distribution": "codegen-kit-notes-digest",
            "version": "0.1.0",
        },
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "tooling_commit": "2" * 40,
    }


@pytest.mark.asyncio
async def test_a_local_install_is_queued_claimed_and_published_once_without_engineering(
    real_redis, tmp_path
):
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_API_BASE_URL"]
    telegram = uuid.uuid4().int % 1_000_000_000
    before = {queue: await real_redis.xlen(queue) for queue in (ENGINEERING_QUEUE, WORKER_COMMANDS)}
    try:
        await api.post("users/", json={"telegram_id": telegram, "username": "local-owner"})
        project = await api.post(
            "projects/",
            headers={"X-Telegram-ID": str(telegram)},
            json={
                "title": "Owned notes product",
                "status": "active",
                "initiating_run_id": f"local-init-{telegram}",
                "config": {"workspace_ready": True, "modules": ["backend", "tg_bot"]},
            },
        )
        pid = project["id"]
        repository = await api.post(
            "repositories/",
            json={
                "project_id": pid,
                "name": "Notes",
                "git_url": f"https://github.com/ci/notes-{telegram}",
                "is_managed": True,
            },
        )
        story = await api.post("stories/", json={"project_id": pid, "title": "Install"})
        await api.post(f"stories/{story['id']}/start")
        target = _local_install(pid, repository["id"])
        task = await api.post(
            "tasks/",
            json={
                "project_id": pid,
                "repository_id": repository["id"],
                "story_id": story["id"],
                "title": "Install notes digest",
                "type": "install",
                "status": "todo",
                "install": target,
            },
        )

        _service(
            "_catalog_install_scheduler.py",
            PYTHONPATH="/app/scheduler:/app",
            INSTALL_TASK=task["id"],
        )

        admitted = (await api.get(f"tasks/{task['id']}"))["install_operation"]
        queued = [
            (entry_id.decode(), fields[b"data"])
            for entry_id, fields in await real_redis.xrange(SCAFFOLD_QUEUE)
            if json.loads(fields[b"data"]).get("task_id") == task["id"]
        ]
        [(entry_id, data)] = queued
        assert json.loads(data)["install"] == target
        message = ScaffoldMessage.model_validate_json(data)
        assert isinstance(message.install, LocalPackageInstall)
        assert message.install.model_dump(mode="json") == target
        assert (
            message.mode,
            message.project_id,
            message.repository_id,
            message.story_id,
            message.operation_id,
            message.project_name,
        ) == ("install", pid, repository["id"], story["id"], admitted["id"], f"notes-{telegram}")
        assert message.cycle_started_at == datetime.fromisoformat(admitted["cycle_started_at"])

        result = tmp_path / "delivery.json"
        _service(
            "_local_install_scaffolder.py",
            PYTHONPATH="/app/scaffolder:/app",
            SCAFFOLD_ENTRY=entry_id,
            RESULT_FILE=str(result),
            WORKSPACE_BASE_PATH=str(tmp_path),
            GITHUB_ORG="ci",
        )
        delivered = json.loads(result.read_text())

        first, replay = delivered["results"]
        assert first == {"status": "success", "head_sha": HEAD, "operation_id": admitted["id"]}
        assert replay == {"status": "skipped", "operation_id": admitted["id"]}
        assert delivered["executed"] == [
            {
                "project_id": pid,
                "repository_id": repository["id"],
                "story_id": story["id"],
                "operation_id": admitted["id"],
                "install": target,
                "git_url": repository["git_url"],
            }
        ]
        assert delivered["fenced"] == ["preflight", "commit", "readback", "publish"]
        settled = await api.get(f"tasks/{task['id']}")
        operation = settled["install_operation"]
        assert settled["status"] == "done" and settled["install"] == target
        assert (operation["id"], operation["state"]) == (admitted["id"], "published")
        assert (operation["base_sha"], operation["head_sha"]) == ("1" * 40, HEAD)
        assert operation["verification"]["mode"] == "readback"
        assert operation["verification"]["target"] == target
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
