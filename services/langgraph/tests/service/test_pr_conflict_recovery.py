"""Registered owner tool, poller and dispatcher retain real Task/Run history."""

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, patch
import uuid

from psycopg import AsyncConnection
from psycopg.rows import dict_row
import pytest

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorDiagnosticSnapshot,
    safe_executor_diagnostic_reason,
)
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import unprotect_po_payload
from shared.queues import ENGINEERING_QUEUE, PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from src.clients.api import LanggraphAPIClient


def scheduler(mode: str, story_id: str):
    env = os.environ | {
        "PYTHONPATH": "/app/scheduler:/app",
        "API_BASE_URL": os.environ["TEST_CONFLICT_API_BASE_URL"],
        "CONFLICT_MODE": mode,
        "CONFLICT_STORY": story_id,
    }
    result = subprocess.run(
        [sys.executable, "-P", str(Path(__file__).with_name("_pr_conflict_scheduler.py"))],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def rows(query, *args):
    async with await AsyncConnection.connect(
        os.environ["TEST_DATABASE_URL"], row_factory=dict_row
    ) as db:
        return await (await db.execute(query, args)).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("released", [False, True])
@pytest.mark.parametrize(
    "ending",
    [
        "dirty",
        "clean",
        "gave_up",
        "gave_up-lost",
        "gave_up-concurrent",
        "gave_up-todo",
        "failed",
    ],
)
async def test_dirty_pr_reaches_real_admitted_run_and_durable_exhaustion(
    real_redis, released, ending
):
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_CONFLICT_API_BASE_URL"]
    telegram = uuid.uuid4().int % 1_000_000_000
    try:
        await api.post("users/", json={"telegram_id": telegram, "username": "conflict-owner"})
        project = await api.post(
            "projects/",
            headers={"X-Telegram-ID": str(telegram)},
            json={
                "title": "Conflict recovery",
                "status": "active",
                "initiating_run_id": "fixture-init",
                "config": {"workspace_ready": True},
            },
        )
        story = await api.post("stories/", json={"project_id": project["id"], "title": "Conflict"})
        sid = story["id"]
        await api.post(
            "repositories/",
            json={
                "project_id": project["id"],
                "name": sid,
                "git_url": f"https://github.com/synthetic/{sid}",
                "role": "primary",
            },
        )
        await api.transition_story(sid, "start")
        await api.transition_story(sid, "pr_review")
        await api.patch(f"stories/{sid}", json={"pr_number": 3})
        original = await api.post(
            "tasks/",
            json={
                "project_id": project["id"],
                "story_id": sid,
                "title": "Original",
                "status": "done",
                "type": "feature",
            },
        )
        if released:
            await api.patch(
                f"stories/{sid}",
                json={
                    "quarantine_reason": {
                        "reason": "github_app_merge_refused",
                        "mergeable_state": "dirty",
                        "pr_number": 3,
                    }
                },
            )
            await api.transition_story(sid, "human-review")
            from src.agents.po import tools_stories
            from src.agents.po.tools import get_all_tools

            registered = next(tool for tool in get_all_tools() if tool.name == "reopen_story")
            with patch.object(tools_stories, "_get_api", return_value=api):
                response = await registered.ainvoke(
                    {"story_id": sid}, config={"configurable": {"telegram_chat_id": str(telegram)}}
                )
            assert "conflict repair" in response
        else:
            await asyncio.to_thread(scheduler, "poll", sid)
        tasks = await rows("SELECT * FROM tasks WHERE story_id=%s ORDER BY id", sid)
        repair = next(row for row in tasks if row["id"] != original["id"])
        assert len(tasks) == 2 and repair["type"] == "fix" and repair["status"] == "todo"
        assert (await api.get_story(sid)).reopened_at is None
        now = datetime.now(UTC)
        diagnostics = [
            {
                "executor": agent,
                "enabled": True,
                "auth_mode": "host_session",
                "availability": "available",
                "observed_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "active_lease_count": 0,
                "reason_code": "ready",
                "reason": safe_executor_diagnostic_reason("ready"),
                "profile": {
                    "condition": "healthy",
                    "login_state": "logged_in",
                    "refresh_material": "present",
                },
            }
            for agent in ("claude", "codex")
        ]
        snapshot = ExecutorDiagnosticSnapshot.model_validate(
            {
                "schema_version": "v2",
                "version": "conflict-test",
                "observed_at": now,
                "expires_at": now + timedelta(minutes=5),
                "diagnostics": diagnostics,
            }
        )
        await real_redis.set(EXECUTOR_DIAGNOSTICS_REDIS_KEY, snapshot.model_dump_json())
        await asyncio.to_thread(
            scheduler, "dispatch-interrupt" if ending == "gave_up-todo" else "dispatch", sid
        )
        runs = await rows("SELECT * FROM runs WHERE task_id=%s", repair["id"])
        assert len(runs) == 1 and runs[0]["story_id"] == sid
        messages = [
            EngineeringMessage.model_validate_json(fields[b"data"])
            for _, fields in await real_redis.xrange(ENGINEERING_QUEUE)
        ]
        messages = [message for message in messages if message.story_id == sid]
        assert len(messages) == 1 and messages[0].planning_task_id == repair["id"]
        assert messages[0].task_id == runs[0]["id"]
        assert messages[0].branch == f"story/{sid}"
        await finish_repair(api, sid, project["id"], repair, runs[0], ending)
        if ending == "clean":
            await assert_clean_merge(api, sid, original["id"])
            return
        stopped = await api.get_story(sid)
        assert stopped.status.value == "waiting_human_review"
        assert stopped.quarantine_reason["code"] == "pr_conflict_repair_exhausted"
        assert repair["id"] in stopped.quarantine_reason["detail"]
        notice = await api.get(f"stories/{sid}/owner-notification")
        assert notice["state"] == "owed"
        await asyncio.to_thread(scheduler, "notice", sid)
        stopped = await api.get_story(sid)
        notice = await api.get(f"stories/{sid}/owner-notification")
        assert notice["state"] == "delivered"
        assert notice["admin_state"] == "delivered"
        notices = [
            unprotect_po_payload(PO_INPUT_QUEUE, fields)
            for _, fields in await real_redis.xrange(PO_INPUT_QUEUE)
        ]
        assert any(notice.get("story_id") == sid for notice in notices)
        if ending.startswith("gave_up"):
            await assert_delivered_notice_is_stable(api, real_redis, sid, notice)
        assert len(await rows("SELECT id FROM tasks WHERE story_id=%s", sid)) == 2
        assert (await api.get_task(original["id"])).status.value == "done"
        expected_runs = repair["max_iterations"] + 1 if ending == "failed" else 1
        assert (
            len(await rows("SELECT id FROM runs WHERE task_id=%s", repair["id"])) == expected_runs
        )
    finally:
        await api.close()


async def assert_delivered_notice_is_stable(api, redis, sid, notice):
    notices_before = await redis.xrange(PO_INPUT_QUEUE)
    await asyncio.to_thread(scheduler, "stuck", sid)
    await asyncio.to_thread(scheduler, "notice-repeat", sid)
    assert await api.get(f"stories/{sid}/owner-notification") == notice
    assert await redis.xrange(PO_INPUT_QUEUE) == notices_before


async def assert_clean_merge(api, sid, original_id):
    merged = await api.get_story(sid)
    assert merged.status.value == "pr_review"
    timeline = merged.generated_product_timeline
    assert timeline["pull_request"]["merge_commit_sha"] == "c" * 40
    assert timeline["pull_request"]["state"] == "closed"
    assert timeline["latest_ci_observation"]["ci_run_id"] is None
    assert "deploy_observation" not in timeline
    assert not await rows("SELECT id FROM runs WHERE story_id=%s AND type='deploy'", sid)
    assert len(await rows("SELECT id FROM tasks WHERE story_id=%s", sid)) == 2
    assert (await api.get_task(original_id)).status.value == "done"


async def finish_repair(api, sid, project_id, repair, run, ending):
    if ending.startswith("gave_up"):
        from src.consumers import _base, engineering_result_handler

        stream = RedisStreamClient()
        await stream.connect()
        try:
            group = f"conflict-reclaim-{sid}"
            initial = await claim_engineering_entry(stream, sid, group, reclaim=False)
            assert initial.data["task_id"] == run["id"]
            with patch.object(engineering_result_handler, "api_client", api):
                original_request = api.request

                async def interrupted_request(method, path, **kwargs):
                    if method == "POST" and path.endswith("/attempt-outcome"):
                        if ending == "gave_up-lost":
                            await original_request(method, path, **kwargs)
                        raise asyncio.CancelledError("consumer dies at settlement boundary")
                    return await original_request(method, path, **kwargs)

                with (
                    patch.object(api, "request", interrupted_request),
                    pytest.raises(asyncio.CancelledError, match="settlement boundary"),
                ):
                    await engineering_result_handler.handle_worker_gave_up(
                        run["id"],
                        project_id,
                        repair["id"],
                        sid,
                        "Conflicts need a human",
                        "",
                        stream,
                    )
            persisted_run = await api.get(f"runs/{run['id']}")
            assert persisted_run["status"] == "failed"
            assert persisted_run["result"]["engineering_status"] == "gave_up"
            if ending != "gave_up-lost":
                assert (await api.get_task(repair["id"])).status.value == (
                    "todo" if ending == "gave_up-todo" else "in_dev"
                )
                story = await api.get_story(sid)
                assert story.status.value == "in_progress"
                assert (await rows("SELECT owner_notification FROM stories WHERE id=%s", sid))[0][
                    "owner_notification"
                ] is None
            # Native Redis reclaim and the real consumer guard ACK terminal work
            # without publishing another turn or manually replaying its handler.
            reclaimed = await claim_engineering_entry(stream, sid, group, reclaim=True)
            assert reclaimed.reclaimed and reclaimed.message_id == initial.message_id
            process = AsyncMock()
            with patch.object(_base, "api_client", api):
                await _base._process_entry(
                    reclaimed,
                    stream,
                    ENGINEERING_QUEUE,
                    group,
                    "engineering",
                    process,
                )
            process.assert_not_awaited()
            assert not await stream.redis.xpending_range(
                ENGINEERING_QUEUE,
                group,
                initial.message_id,
                initial.message_id,
                1,
            )
            await asyncio.to_thread(
                scheduler,
                "dispatch"
                if ending == "gave_up-todo"
                else "stuck-concurrent-lost"
                if ending == "gave_up-concurrent"
                else "stuck",
                sid,
            )
            stopped = await api.get_story(sid)
            notice = await api.get(f"stories/{sid}/owner-notification")
            assert notice["state"] == notice["admin_state"] == "owed"
            assert (
                f"PR #3: repair Task {repair['id']}, Run {run['id']}"
                in stopped.quarantine_reason["detail"]
            )
            assert f"ceiling {repair['max_iterations']}" in stopped.quarantine_reason["detail"]
            assert (
                len(
                    await rows(
                        "SELECT id FROM task_events WHERE task_id=%s "
                        "AND details::jsonb ? 'pr_conflict_repair_attempt'",
                        repair["id"],
                    )
                )
                == 1
            )
            await asyncio.to_thread(scheduler, "stuck", sid)
            assert await api.get(f"stories/{sid}/owner-notification") == notice
            assert await api.get(f"runs/{run['id']}") == persisted_run
        finally:
            await stream.close()
        assert (await api.get_task(repair["id"])).status.value == "waiting_human_review"
    elif ending == "failed":
        for iteration in range(repair["max_iterations"] + 1):
            if iteration:
                await asyncio.to_thread(scheduler, "dispatch", sid)
                run = (
                    await rows(
                        "SELECT * FROM runs WHERE task_id=%s AND status='queued'", repair["id"]
                    )
                )[0]
            await api.patch(
                f"runs/{run['id']}",
                json={"status": "failed", "result": {"engineering_status": "failed"}},
            )
            await api.post(f"tasks/{repair['id']}/fail")
            await asyncio.to_thread(scheduler, "fail-lost" if iteration == 0 else "fail", sid)
            task = await api.get_task(repair["id"])
            assert task.current_iteration == min(iteration + 1, repair["max_iterations"])
            assert task.status.value == (
                "todo" if iteration < repair["max_iterations"] else "waiting_human_review"
            )
    else:
        await api.patch(
            f"runs/{run['id']}",
            json={
                "status": "completed",
                "result": {"engineering_status": "done", "commit_sha": "a" * 40},
            },
        )
        await api.post(f"tasks/{repair['id']}/complete")
        await api.transition_story(sid, "pr_review")
        await asyncio.to_thread(scheduler, "clean" if ending == "clean" else "poll", sid)


async def claim_engineering_entry(stream, sid, group, *, reclaim):
    async with (
        asyncio.timeout(5),
        aclosing(
            stream.consume(
                ENGINEERING_QUEUE,
                group,
                "replacement" if reclaim else "interrupted",
                auto_ack=False,
                claim_pending=reclaim,
                pending_timeout_ms=0,
                block_ms=10,
            )
        ) as entries,
    ):
        async for entry in entries:
            if entry is not None and entry.data.get("story_id") == sid:
                return entry
    raise AssertionError("The admitted engineering entry was not discovered")
