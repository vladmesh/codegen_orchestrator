"""Pipeline test: Engineering phase (~3-5 min).

Exercises: scaffold → story/task → task_dispatcher → engineering:queue
         → noop worker → empty commit + push → CI → task done → story complete.

No deploy. Verifies the engineering pipeline works end-to-end.
"""

import httpx
from live_harness import cleanup_guard
from pipeline_helpers import (
    API_URL,
    AUTH_HEADERS,
    ENGINEERING_TIMEOUT,
    SCAFFOLD_TIMEOUT,
    cleanup_all,
    create_noop_project,
    create_story_and_task,
    dump_debug,
    ensure_test_user,
    internal_headers,
    trigger_scaffold,
    wait_engineering,
    wait_scaffold,
)
import pytest
import pytest_asyncio

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engineering_ctx():
    """Engineering pipeline: scaffold → story/task → noop worker → CI → done."""
    async with (
        httpx.AsyncClient(base_url=API_URL, timeout=10, headers=AUTH_HEADERS) as api,
        httpx.AsyncClient(base_url=API_URL, timeout=10, headers=internal_headers()) as api_internal,
    ):
        await ensure_test_user(api)
        ctx = await create_noop_project(api, api_internal)
        async with cleanup_guard(
            lambda: cleanup_all(api_internal, None, ctx), manifest=ctx["manifest"]
        ):
            # Phase 1: Scaffold
            trigger_scaffold(ctx)
            await wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT)
            if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
                yield ctx
                dump_debug(ctx, "engineering-scaffold")
                return

            # Phase 2: Engineering
            await create_story_and_task(api, ctx)
            await wait_engineering(api, ctx, timeout=ENGINEERING_TIMEOUT)

            yield ctx

            if ctx.get("task_status") != TaskStatus.DONE:
                dump_debug(ctx, "engineering")


class TestEngineeringPipeline:
    """Engineering pipeline: scaffold → noop worker → CI → task done."""

    async def test_scaffold_passed(self, engineering_ctx):
        """Scaffold must succeed before engineering can run."""
        assert engineering_ctx.get("scaffold_status") == ProjectStatus.ACTIVE, (
            f"Scaffold failed — status: {engineering_ctx.get('scaffold_status')}"
        )

    async def test_task_completed(self, engineering_ctx):
        """Task reaches 'done' (worker succeeded + CI passed)."""
        if engineering_ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
            pytest.skip("scaffold failed")
        assert engineering_ctx.get("task_status") == TaskStatus.DONE, (
            f"Task not done — status: {engineering_ctx.get('task_status')} "
            f"after {engineering_ctx.get('engineering_elapsed', '?')}s"
        )

    async def test_story_progressed(self, engineering_ctx):
        """Story transitions past in_progress after all tasks done.

        With the PR-based CI gate, stories go to 'pr_review' after all tasks done
        (dispatcher creates PR from story branch → main with auto-merge).
        'deploying' happens later via webhook when PR is merged.
        """
        if engineering_ctx.get("task_status") != TaskStatus.DONE:
            pytest.skip("task not done")
        assert engineering_ctx.get("story_status") in {
            StoryStatus.PR_REVIEW,
            StoryStatus.DEPLOYING,
            StoryStatus.COMPLETED,
        }, f"Story not progressed — status: {engineering_ctx.get('story_status')}"
