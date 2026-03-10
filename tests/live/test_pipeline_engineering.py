"""Pipeline test: Engineering phase (~3-5 min).

Exercises: scaffold → story/task → task_dispatcher → engineering:queue
         → noop worker → empty commit + push → CI → task done → story complete.

No deploy. Verifies the engineering pipeline works end-to-end.
"""

import httpx
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
    flush_queues,
    trigger_scaffold,
    wait_engineering,
    wait_scaffold,
)
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engineering_ctx():
    """Engineering pipeline: scaffold → story/task → noop worker → CI → done."""
    flush_queues()
    async with httpx.AsyncClient(base_url=API_URL, timeout=10, headers=AUTH_HEADERS) as api:
        await ensure_test_user(api)
        ctx = await create_noop_project(api)

        # Phase 1: Scaffold
        trigger_scaffold(ctx)
        await wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT)
        if ctx.get("scaffold_status") != "scaffolded":
            yield ctx
            dump_debug(ctx, "engineering-scaffold")
            await cleanup_all(api, None, ctx)
            return

        # Phase 2: Engineering
        await create_story_and_task(api, ctx)
        await wait_engineering(api, ctx, timeout=ENGINEERING_TIMEOUT)

        yield ctx

        if ctx.get("task_status") != "done":
            dump_debug(ctx, "engineering")
        await cleanup_all(api, None, ctx)


class TestEngineeringPipeline:
    """Engineering pipeline: scaffold → noop worker → CI → task done."""

    async def test_scaffold_passed(self, engineering_ctx):
        """Scaffold must succeed before engineering can run."""
        assert engineering_ctx.get("scaffold_status") == "scaffolded", (
            f"Scaffold failed — status: {engineering_ctx.get('scaffold_status')}"
        )

    async def test_task_completed(self, engineering_ctx):
        """Task reaches 'done' (worker succeeded + CI passed)."""
        if engineering_ctx.get("scaffold_status") != "scaffolded":
            pytest.skip("scaffold failed")
        assert engineering_ctx.get("task_status") == "done", (
            f"Task not done — status: {engineering_ctx.get('task_status')} "
            f"after {engineering_ctx.get('engineering_elapsed', '?')}s"
        )

    async def test_story_completed(self, engineering_ctx):
        """Story transitions to 'complete' after all tasks done."""
        if engineering_ctx.get("task_status") != "done":
            pytest.skip("task not done")
        assert engineering_ctx.get("story_status") == "completed", (
            f"Story not complete — status: {engineering_ctx.get('story_status')}"
        )
