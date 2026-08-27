"""Two users' projects work at the same time, and neither is worked on twice.

This is the narrow proof for `engineering.worker_slots`: admission has always
been willing to let several paid runs exist, but the engineering consumer ran
them one after another, so the second user waited out the first user's whole
turn. The claim under test is only about the consumer — two projects, two
engineering runs, overlapping in wall-clock time — so it uses the mechanical
noop worker and never reaches a deploy.

It also states the thing that makes parallel consumption safe: a PEL sweep runs
while jobs are in flight, and an entry that comes back from it must not become a
second execution of live work. Two runs per task would show up here as a task
carrying more than one engineering run.
"""

from __future__ import annotations

import asyncio

from live_harness import cleanup_guard
from pipeline_helpers import (
    ENGINEERING_TIMEOUT,
    SCAFFOLD_TIMEOUT,
    api_client_as_internal_service,
    api_client_as_test_user,
    cleanup_all,
    create_noop_project,
    create_story_and_task,
    ensure_test_user,
    trigger_scaffold,
    wait_engineering,
    wait_scaffold,
)
import pytest

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus

pytestmark = pytest.mark.asyncio(loop_scope="module")

# What the issue asks to see proved. A noop turn is short, so this is the
# overlap that shows the consumer is genuinely concurrent rather than the
# accidental overlap of two adjacent runs.
MIN_OVERLAP_SECONDS = 5


def _interval(run: dict) -> tuple[str, str] | None:
    started, completed = run.get("started_at"), run.get("completed_at")
    return (started, completed) if started and completed else None


async def _engineering_runs(api_internal, project_id: str) -> list[dict]:
    resp = await api_internal.get("/api/runs/", params={"project_id": project_id})
    resp.raise_for_status()
    return [r for r in resp.json() if r.get("type") == "engineering"]


@pytest.mark.asyncio()
async def test_two_projects_run_engineering_at_the_same_time():
    async with (
        api_client_as_test_user() as api,
        api_client_as_internal_service() as api_internal,
    ):
        await ensure_test_user(api, api_internal)
        first = await create_noop_project(api, api_internal)
        second = await create_noop_project(api, api_internal)

        async with (
            cleanup_guard(
                lambda: cleanup_all(api_internal, None, first), manifest=first["manifest"]
            ),
            cleanup_guard(
                lambda: cleanup_all(api_internal, None, second), manifest=second["manifest"]
            ),
        ):
            for ctx in (first, second):
                trigger_scaffold(ctx)
            await asyncio.gather(
                *(wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT) for ctx in (first, second))
            )
            for ctx in (first, second):
                assert ctx.get("scaffold_status") == ProjectStatus.ACTIVE, (
                    f"scaffold did not finish for {ctx['project_id']}"
                )

            # Both stories are created before either is waited on: the point is
            # that the second does not queue behind the first.
            for ctx in (first, second):
                await create_story_and_task(api, ctx)

            await asyncio.gather(
                *(
                    wait_engineering(api, ctx, timeout=ENGINEERING_TIMEOUT)
                    for ctx in (first, second)
                )
            )

            for ctx in (first, second):
                assert ctx.get("task_status") == TaskStatus.DONE, (
                    f"engineering did not finish for {ctx['project_id']}: {ctx.get('task_status')}"
                )

            runs = {
                ctx["project_id"]: await _engineering_runs(api_internal, ctx["project_id"])
                for ctx in (first, second)
            }

            # No task is executed twice. A reclaimed entry that took over live
            # work would leave a second engineering run against the same task.
            for project_id, project_runs in runs.items():
                by_task: dict[str, int] = {}
                for run in project_runs:
                    by_task[run.get("task_id")] = by_task.get(run.get("task_id"), 0) + 1
                repeated = {task: count for task, count in by_task.items() if count > 1}
                assert not repeated, (
                    f"project {project_id} ran a task more than once: {repeated}; "
                    "a PEL sweep took over work that was still live"
                )

            intervals = []
            for project_runs in runs.values():
                assert project_runs, "no engineering run recorded"
                interval = _interval(project_runs[0])
                assert interval, f"engineering run has no interval: {project_runs[0]}"
                intervals.append(interval)

            from datetime import datetime

            def _at(value: str) -> datetime:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))

            (a_start, a_end), (b_start, b_end) = intervals
            overlap = (
                min(_at(a_end), _at(b_end)) - max(_at(a_start), _at(b_start))
            ).total_seconds()
            assert overlap >= MIN_OVERLAP_SECONDS, (
                f"engineering runs did not overlap ({overlap:.1f}s): "
                f"{a_start}..{a_end} and {b_start}..{b_end}. "
                "The consumer is still working one entry at a time."
            )
