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
        "failed-late-start",
        "budget-first",
        "budget-retry",
    ],
)
async def test_dirty_pr_reaches_real_admitted_run_and_durable_exhaustion(  # noqa: PLR0915 - native recovery cases
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
        if ending.startswith("budget-"):
            await exercise_budget_refusal(
                api, real_redis, sid, project, repair, original["id"], ending
            )
            return
        delayed_dispatch = await begin_dispatch(real_redis, sid, ending)
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
        await finish_admitted_repair(
            api, real_redis, sid, project["id"], repair, runs[0], ending, delayed_dispatch
        )
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
        if ending.startswith("gave_up") or ending == "failed-late-start":
            await assert_delivered_notice_is_stable(api, real_redis, sid, notice)
        assert len(await rows("SELECT id FROM tasks WHERE story_id=%s", sid)) == 2
        assert (await api.get_task(original["id"])).status.value == "done"
        expected_runs = repair["max_iterations"] + 1 if ending.startswith("failed") else 1
        assert (
            len(await rows("SELECT id FROM runs WHERE task_id=%s", repair["id"])) == expected_runs
        )
    finally:
        await api.close()


def assert_refusal_preserves_task_history(before_events, events, expected_details):
    # Atomic retries share created_at; the events API does not order timestamp ties.
    before_by_id = {event["id"]: event for event in before_events}
    after_by_id = {event["id"]: event for event in events}
    assert len(before_by_id) == len(before_events)
    assert len(after_by_id) == len(events)
    assert before_by_id.keys() <= after_by_id.keys()
    assert all(after_by_id[event_id] == event for event_id, event in before_by_id.items())
    added = [event for event in events if event["id"] not in before_by_id]
    assert len(added) == 2
    for source, target, actor in [
        ("todo", "in_dev", "internal_service"),
        ("in_dev", "waiting_human_review", "dispatcher"),
    ]:
        matching = [
            event
            for event in added
            if (event["from_status"], event["to_status"]) == (source, target)
        ]
        assert len(matching) == 1
        event = matching[0]
        assert event["task_id"] == expected_details["engineering_dispatch_refusal"]["task_id"]
        assert event["event_type"] == "status_change" and event["iteration"] is None
        assert event["actor"] == actor and event["details"] == expected_details


async def exercise_budget_refusal(api, redis, sid, project, repair, original_id, ending):
    url = f"engineering-budget-policies/{project['owner_id']}"
    policy = await set_budget_policy(
        api,
        url,
        json={"limit_microusd": 20, "attempt_reservation_microusd": 10, "state": "enabled"},
    )
    later = ending == "budget-retry"
    if later:
        # Real Redis publication/consumer failure/atomic retry, including the
        # original delayed admitted start, precede this later no-Run denial.
        delayed = await begin_dispatch(redis, sid, "failed-late-start")
        run = (await rows("SELECT * FROM runs WHERE task_id=%s", repair["id"]))[0]
        await fail_before_original_start(api, redis, sid, repair, run, delayed)
    before_runs = await rows(
        "SELECT * FROM runs WHERE task_id=%s ORDER BY created_at", repair["id"]
    )
    before_events = await api.get(f"tasks/{repair['id']}/events")
    policy = await set_budget_policy(
        api,
        url,
        json={
            "limit_microusd": 0,
            "attempt_reservation_microusd": 10,
            "state": "enabled",
            "version": policy["version"],
        },
    )
    publications = await redis.xrange(ENGINEERING_QUEUE)
    await asyncio.to_thread(scheduler, "refusal-lost", sid)
    task = await api.get_task(repair["id"])
    story = await api.get_story(sid)
    assert task.status.value == story.status.value == "waiting_human_review"
    assert task.current_iteration == int(later) and task.max_iterations == repair["max_iterations"]
    audits = await rows(
        "SELECT * FROM work_admission_audits WHERE subject='paid_work' "
        "AND reason='engineering_budget_denied' AND command_payload->>'task_id'=%s",
        repair["id"],
    )
    assert len(audits) == 1
    did = audits[0]["reference_id"]
    assert (
        repair["id"] in story.quarantine_reason["detail"]
        and did in story.quarantine_reason["detail"]
    )
    assert not await rows("SELECT id FROM runs WHERE id=%s", did)
    budget = await api.get(f"engineering-budget-policies/admissions/{did}")
    assert (
        budget["outcome"] == "denied"
        and budget["reservation_state"] is None
        and budget["active_held_microusd"] == 0
    )
    assert (
        await rows("SELECT * FROM runs WHERE task_id=%s ORDER BY created_at", repair["id"])
        == before_runs
    )
    events = await api.get(f"tasks/{repair['id']}/events")
    detail = (
        f"PR #{story.pr_number} repair Task {repair['id']} is waiting for engineering "
        f"budget (decision {did}). {audits[0]['message']}"
    )
    assert story.quarantine_reason["detail"] == detail
    assert_refusal_preserves_task_history(
        before_events,
        events,
        {
            "engineering_dispatch_refusal": {
                "task_id": repair["id"],
                "decision_id": did,
                "reason": "engineering_budget_denied",
            },
            "detail": detail,
            # The admission records capacity at decision time; the GET deliberately
            # omits it. This fixture's enabled zero limit gives exactly zero capacity.
            "engineering_budget": budget | {"available_microusd": 0},
        },
    )
    assert await redis.xrange(ENGINEERING_QUEUE) == publications
    notice = await api.get(f"stories/{sid}/owner-notification")
    assert notice["state"] == notice["admin_state"] == "owed"
    await asyncio.to_thread(scheduler, "notice-sweep", sid)
    notice = await api.get(f"stories/{sid}/owner-notification")
    assert notice["state"] == notice["admin_state"] == "delivered"
    await assert_delivered_notice_is_stable(api, redis, sid, notice)
    notices = [
        unprotect_po_payload(PO_INPUT_QUEUE, fields)
        for _, fields in await redis.xrange(PO_INPUT_QUEUE)
    ]
    assert sum(n.get("story_id") == sid for n in notices) == 1
    await set_budget_policy(
        api,
        url,
        json={
            "limit_microusd": 100,
            "attempt_reservation_microusd": 10,
            "state": "enabled",
            "version": policy["version"],
        },
    )
    # Existing authenticated internal/admin recovery deliberately grants the
    # next iteration and ceiling; money alone cannot resume a parked Task.
    refused = await api.post(
        "work-admission/engineering-dispatches", json={"task_id": repair["id"]}
    )
    assert refused["reason"] == "task_not_dispatchable"
    resumed = await api.post(
        f"tasks/{repair['id']}/resume", json={"guidance": "Policy capacity restored", "retries": 4}
    )
    assert (
        resumed["current_iteration"] == int(later) + 1
        and resumed["max_iterations"] == int(later) + 5
    )
    await asyncio.to_thread(scheduler, "dispatch", sid)
    after = await rows("SELECT * FROM runs WHERE task_id=%s ORDER BY created_at", repair["id"])
    assert len(after) == len(before_runs) + 1 and after[:-1] == before_runs
    assert after[-1]["metadata"]["iteration"] == int(later) + 1
    messages = [
        EngineeringMessage.model_validate_json(fields[b"data"])
        for _, fields in await redis.xrange(ENGINEERING_QUEUE)
    ]
    messages = [message for message in messages if message.story_id == sid]
    assert len(messages) == len(after) and {m.task_id for m in messages} == {r["id"] for r in after}
    assert len(await rows("SELECT id FROM tasks WHERE story_id=%s", sid)) == 2
    assert (await api.get_task(original_id)).status.value == "done"
    assert await api.get(f"stories/{sid}/owner-notification") == notice


async def set_budget_policy(api, url, **kwargs):
    return (await api.request("PUT", url, **kwargs)).json()


async def assert_delivered_notice_is_stable(api, redis, sid, notice):
    notices_before = await redis.xrange(PO_INPUT_QUEUE)
    await asyncio.to_thread(scheduler, "stuck", sid)
    await asyncio.to_thread(scheduler, "notice-repeat", sid)
    assert await api.get(f"stories/{sid}/owner-notification") == notice
    assert await redis.xrange(PO_INPUT_QUEUE) == notices_before


async def begin_dispatch(redis, sid, ending):
    if ending == "failed-late-start":
        delayed = asyncio.create_task(asyncio.to_thread(scheduler, "dispatch-pause-start", sid))
        assert await redis.blpop(f"conflict-start-ready:{sid}", timeout=10)
        return delayed
    await asyncio.to_thread(
        scheduler, "dispatch-interrupt" if ending == "gave_up-todo" else "dispatch", sid
    )
    return None


async def finish_admitted_repair(api, redis, sid, pid, repair, run, ending, delayed):
    if delayed is None:
        await finish_repair(api, sid, pid, repair, run, ending)
        return
    await fail_before_original_start(api, redis, sid, repair, run, delayed)
    await asyncio.to_thread(scheduler, "dispatch-start-lost", sid)
    next_runs = await rows(
        "SELECT id, metadata FROM runs WHERE task_id=%s ORDER BY created_at", repair["id"]
    )
    assert len(next_runs) == 2
    # Raw PostgreSQL rows use the physical column, unlike the API's run_metadata.
    assert next_runs[0]["metadata"]["iteration"] == 0
    assert next_runs[1]["metadata"]["iteration"] == 1
    messages = [
        EngineeringMessage.model_validate_json(fields[b"data"])
        for _, fields in await redis.xrange(ENGINEERING_QUEUE)
    ]
    messages = [message for message in messages if message.story_id == sid]
    assert len(messages) == 2
    assert {message.task_id for message in messages} == {run["id"] for run in next_runs}
    await finish_repair(api, sid, pid, repair, next_runs[1], "failed")


async def fail_before_original_start(api, redis, sid, repair, run, delayed_dispatch):
    from src.consumers import _base, engineering_result_handler

    stream = RedisStreamClient()
    await stream.connect()
    try:
        assert (await api.get_task(repair["id"])).status.value == "todo"
        group = f"conflict-fast-failure-{sid}"
        entry = await claim_engineering_entry(stream, sid, group, reclaim=False)
        assert entry.data["task_id"] == run["id"]

        async def early_failure(data, connection):
            return await engineering_result_handler.fail_job(
                data["task_id"],
                "Early technical failure",
                data["planning_task_id"],
                redis=connection,
                story_id=data["story_id"],
                turn_result_consumed=True,
            )

        with (
            patch.object(_base, "api_client", api),
            patch.object(engineering_result_handler, "api_client", api),
        ):
            await _base._process_entry(
                entry, stream, ENGINEERING_QUEUE, group, "engineering", early_failure
            )
        task = await api.get_task(repair["id"])
        assert (task.status.value, task.current_iteration) == ("todo", 1)
        persisted = await api.get(f"runs/{run['id']}")
        assert persisted["status"] == "failed" and persisted["run_metadata"]["iteration"] == 0
        events = await api.get(f"tasks/{repair['id']}/events")
        assert sum("pr_conflict_repair_attempt" in e["details"] for e in events) == 1
        publications = await redis.xrange(ENGINEERING_QUEUE)
        await redis.rpush(f"conflict-start-release:{sid}", "release")
        await delayed_dispatch
        task = await api.get_task(repair["id"])
        assert (task.status.value, task.current_iteration) == ("todo", 1)
        await asyncio.to_thread(scheduler, "stuck", sid)
        assert await api.get(f"runs/{run['id']}") == persisted
        assert await redis.xrange(ENGINEERING_QUEUE) == publications
        assert (await api.get_story(sid)).status.value == "in_progress"
        assert (await rows("SELECT owner_notification FROM stories WHERE id=%s", sid))[0][
            "owner_notification"
        ] is None
    finally:
        # Always release and join the finite CI fixture process on assertion failure.
        await redis.rpush(f"conflict-start-release:{sid}", "release")
        await delayed_dispatch
        await stream.close()


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
        first_iteration = (await api.get_task(repair["id"])).current_iteration
        for iteration in range(first_iteration, repair["max_iterations"] + 1):
            if iteration > first_iteration:
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
