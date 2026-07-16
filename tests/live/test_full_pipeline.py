"""Pipeline test: Full deploy (~7-10 min) — THE MEGA TEST.

Exercises the entire path from project creation to a live /health response:

  1. API: create project (agent_type=noop) + repo
  2. scaffold:queue → scaffolder → GitHub repo (+ branch protection on main)
  3. API: create story (in_progress) + task (todo)
  4. task_dispatcher → engineering:queue → noop worker → empty commit + push to story branch
  5. All tasks done → dispatcher creates PR story/{id} → main (auto-merge enabled)
  6. CI runs on PR → green → auto-merge → webhook → deploy:queue
  7. deploy consumer → DevOps subgraph → GitHub Actions deploy.yml
  8. smoke test: GET /health → 200

No LLM. Fully deterministic. Real queues, real GitHub, real server.
"""

import asyncio

import httpx
from live_harness import cleanup_guard, run_non_llm_qa
from pipeline_helpers import (
    API_URL,
    AUTH_HEADERS,
    DEPLOY_TIMEOUT,
    ENGINEERING_TIMEOUT,
    SCAFFOLD_TIMEOUT,
    cleanup_all,
    create_noop_project,
    create_story_and_task,
    dump_debug,
    ensure_test_user,
    trigger_scaffold,
    wait_deploy,
    wait_engineering,
    wait_scaffold,
)
import pytest
import pytest_asyncio

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def pipeline():
    """Full pipeline: scaffold → engineering → deploy. Yields context for assertions."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=10, headers=AUTH_HEADERS) as api:
        await ensure_test_user(api)
        async with httpx.AsyncClient(base_url=API_URL, timeout=10) as api_no_auth:
            ctx = await create_noop_project(api)
            async with cleanup_guard(
                lambda: cleanup_all(api, api_no_auth, ctx), manifest=ctx["manifest"]
            ):
                # Phase 1: Scaffold
                trigger_scaffold(ctx)
                await wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT)
                if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
                    yield ctx
                    dump_debug(ctx, "full-scaffold")
                    return

                # Phase 2: Engineering
                await create_story_and_task(api, ctx)
                await wait_engineering(api, ctx, timeout=ENGINEERING_TIMEOUT)
                if ctx.get("task_status") != TaskStatus.DONE:
                    yield ctx
                    dump_debug(ctx, "full-engineering")
                    return

                # Phase 3: Deploy
                await wait_deploy(api, api_no_auth, ctx, timeout=DEPLOY_TIMEOUT)
                if ctx.get("final_app_status") == ApplicationStatus.RUNNING.value:
                    ctx["qa_result"] = await run_non_llm_qa(
                        api_no_auth,
                        ctx["deployed_url"],
                        timeout=DEPLOY_TIMEOUT,
                    )

                yield ctx

                if ctx.get("final_app_status") != ApplicationStatus.RUNNING.value:
                    dump_debug(ctx, "full-deploy")


class TestFullPipeline:
    """THE MEGA TEST: project → scaffold → noop worker → CI → deploy → health check."""

    async def test_project_active(self, pipeline):
        """Project status should be 'active' after successful scaffold + deploy."""
        assert pipeline.get("scaffold_status") == ProjectStatus.ACTIVE, (
            f"Scaffold failed — status: {pipeline.get('scaffold_status')}"
        )
        assert pipeline.get("task_status") == TaskStatus.DONE, (
            f"Engineering failed — task status: {pipeline.get('task_status')}"
        )
        assert pipeline.get("final_app_status") == ApplicationStatus.RUNNING.value, (
            f"Deploy failed — app_status: {pipeline.get('final_app_status')}"
        )

    async def test_port_allocated(self, pipeline):
        """A port should be allocated for the deployed service."""
        if pipeline.get("final_app_status") != ApplicationStatus.RUNNING.value:
            pytest.skip("deploy failed")
        assert "port" in pipeline, "No port allocation found for project"
        assert pipeline["port"] >= 8000, f"Unexpected port: {pipeline['port']}"

    async def test_health_endpoint(self, pipeline):
        """GET /health on deployed service returns 200."""
        if pipeline.get("final_app_status") != ApplicationStatus.RUNNING.value:
            pytest.skip("deploy failed")
        assert "deployed_url" in pipeline, "No deployed_url — port allocation missing?"

        url = pipeline["deployed_url"]
        async with httpx.AsyncClient(timeout=30) as client:
            for _attempt in range(5):
                try:
                    resp = await client.get(f"{url}/health")
                    if resp.status_code == 200:
                        break
                    resp = await client.get(f"{url}/v1/health")
                    if resp.status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(5)
            else:
                pytest.fail(f"Health endpoint not reachable at {url}/health after 5 attempts")

        assert resp.status_code == 200, f"Health check failed: {resp.status_code} {resp.text[:200]}"

    async def test_non_llm_qa_passed(self, pipeline):
        """A separate post-deploy QA run must terminate as passed."""
        if pipeline.get("final_app_status") != ApplicationStatus.RUNNING.value:
            pytest.skip("deploy failed")
        assert pipeline.get("qa_result") == {
            "run_id": pipeline["qa_result"]["run_id"],
            "status": "completed",
            "qa_outcome": "passed",
        }
