"""Pipeline test: Full deploy - THE MEGA TEST.

Exercises the entire path from project creation to a live /health response:

  1. API: create project + repo
  2. scaffold:queue → scaffolder → GitHub repo (+ branch protection on main)
  3. API: create story (in_progress) + task (todo)
  4. task_dispatcher → engineering:queue → worker → commit + push to story branch
  5. All tasks done → dispatcher creates PR story/{id} → main (auto-merge enabled)
  6. CI runs on PR → green → auto-merge → webhook → deploy:queue
  7. deploy consumer → DevOps subgraph → GitHub Actions deploy.yml
  8. smoke test: GET /health → 200

The noop path stays deterministic. The LLM path exercises the product route where a
real developer worker changes code before CI, merge, deploy, health, and QA.
"""

import asyncio

import httpx
from live_harness import cleanup_guard
from pipeline_helpers import (
    API_URL,
    AUTH_HEADERS,
    DEPLOY_OUTCOME_TIMEOUT,
    DEPLOY_RUN_TIMEOUT,
    DEPLOY_TIMEOUT,
    ENGINEERING_TIMEOUT,
    EXPECTED_ENV_CONTRACT_FRAGMENTS,
    LLM_ENGINEERING_TIMEOUT,
    QA_RUN_TIMEOUT,
    SCAFFOLD_TIMEOUT,
    cleanup_all,
    create_llm_backend_project,
    create_noop_project,
    create_story_and_task,
    dump_debug,
    ensure_test_user,
    internal_headers,
    record_env_contract,
    run_non_llm_qa,
    trigger_scaffold,
    wait_deploy,
    wait_deploy_outcome,
    wait_deploy_run,
    wait_engineering,
    wait_scaffold,
)
import pytest
import pytest_asyncio

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _pipeline_run(create_project, *, engineering_timeout: int, debug_prefix: str):
    """Full pipeline: scaffold → engineering → deploy. Yields context for assertions."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=10, headers=AUTH_HEADERS) as api:
        await ensure_test_user(api)
        # Deploy runs belong to no user, and list_runs hides unowned runs from the
        # non-admin harness user, so they are observed through a client that
        # authenticates only as an internal service.
        async with (
            httpx.AsyncClient(base_url=API_URL, timeout=10) as api_no_auth,
            httpx.AsyncClient(
                base_url=API_URL, timeout=10, headers=internal_headers()
            ) as api_internal,
        ):
            ctx = await create_project(api, api_internal)
            async with cleanup_guard(
                lambda: cleanup_all(api_internal, api_no_auth, ctx), manifest=ctx["manifest"]
            ):
                # Phase 1: Scaffold
                trigger_scaffold(ctx)
                await wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT)
                if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
                    yield ctx
                    dump_debug(ctx, f"{debug_prefix}-scaffold")
                    return

                # The scaffolded repo must already carry the contract deploy
                # resolves its environment from.
                if not record_env_contract(ctx, "main", phase="scaffold"):
                    yield ctx
                    dump_debug(ctx, f"{debug_prefix}-env-contract-scaffold")
                    return

                # Phase 2: Engineering
                await create_story_and_task(api, ctx)
                await wait_engineering(api, ctx, timeout=engineering_timeout)
                if ctx.get("task_status") != TaskStatus.DONE:
                    yield ctx
                    dump_debug(ctx, f"{debug_prefix}-engineering")
                    return

                # Phase 3: Deploy. The story branch merges into main and only then
                # does a deploy run appear carrying the merged head SHA. The ref
                # deploy reads the contract at. Re-check the contract there: the
                # scaffolded tree proves nothing about what engineering merged.
                deploy_run = await wait_deploy_run(api_internal, ctx, timeout=DEPLOY_RUN_TIMEOUT)
                if deploy_run is None:
                    yield ctx
                    dump_debug(ctx, f"{debug_prefix}-deploy-run")
                    return
                if not record_env_contract(
                    ctx,
                    ctx["deploy_head_sha"],
                    phase="merged",
                    verify_merged_into_main=True,
                ):
                    yield ctx
                    dump_debug(ctx, f"{debug_prefix}-env-contract-merged")
                    return

                await wait_deploy(api, api_no_auth, ctx, timeout=DEPLOY_TIMEOUT)
                await wait_deploy_outcome(api_internal, ctx, timeout=DEPLOY_OUTCOME_TIMEOUT)
                if (
                    ctx.get("final_app_status") == ApplicationStatus.RUNNING.value
                    and ctx.get("deploy_outcome") == DeployOutcome.SUCCESS.value
                ):
                    ctx["qa_result"] = await run_non_llm_qa(
                        api_internal,
                        ctx["story_id"],
                        timeout=QA_RUN_TIMEOUT,
                    )

                yield ctx

                if (
                    ctx.get("final_app_status") != ApplicationStatus.RUNNING.value
                    or ctx.get("deploy_outcome") != DeployOutcome.SUCCESS.value
                ):
                    dump_debug(ctx, f"{debug_prefix}-deploy")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def pipeline():
    """Full noop pipeline: scaffold → engineering → deploy."""
    async for ctx in _pipeline_run(
        create_noop_project,
        engineering_timeout=ENGINEERING_TIMEOUT,
        debug_prefix="full-noop",
    ):
        yield ctx


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def llm_pipeline():
    """Full LLM pipeline: scaffold → real worker → deploy."""
    async for ctx in _pipeline_run(
        create_llm_backend_project,
        engineering_timeout=LLM_ENGINEERING_TIMEOUT,
        debug_prefix="full-llm",
    ):
        yield ctx


class TestFullPipeline:
    """THE MEGA TEST: project → scaffold → noop worker → CI → deploy → health check."""

    async def test_project_active(self, pipeline):
        """Project status should be 'active' after successful scaffold + deploy."""
        assert pipeline.get("scaffold_status") == ProjectStatus.ACTIVE, (
            f"Scaffold failed, status: {pipeline.get('scaffold_status')}"
        )
        assert pipeline.get("task_status") == TaskStatus.DONE, (
            f"Engineering failed, task status: {pipeline.get('task_status')}"
        )
        assert pipeline.get("final_app_status") == ApplicationStatus.RUNNING.value, (
            f"Deploy failed, app_status: {pipeline.get('final_app_status')}"
        )

    async def test_env_contract_committed_by_scaffold(self, pipeline):
        """The scaffolded repo carries the contract fragments deploy requires."""
        if pipeline.get("scaffold_status") != ProjectStatus.ACTIVE:
            pytest.skip("scaffold failed")
        errors = pipeline.get("env_contract_errors") or {}
        assert "scaffold" not in errors, errors.get("scaffold")
        probe = pipeline["env_contract_probes"]["scaffold"]
        assert set(probe["fragment_paths"]) >= EXPECTED_ENV_CONTRACT_FRAGMENTS
        assert probe["entries"], "scaffolded contract declares no entries"

    async def test_env_contract_present_on_merged_sha(self, pipeline):
        """The contract also holds on the SHA deploy actually resolves it at.

        Deploy reads the contract at the merged head SHA, not at the scaffolded
        tree, so a fragment lost or broken during engineering only shows here.
        """
        if pipeline.get("task_status") != TaskStatus.DONE:
            pytest.skip("engineering failed")
        assert pipeline.get("deploy_run_error") is None, pipeline["deploy_run_error"]
        errors = pipeline.get("env_contract_errors") or {}
        assert "merged" not in errors, errors.get("merged")
        probe = pipeline["env_contract_probes"]["merged"]
        assert probe["ref"] == pipeline["deploy_head_sha"]
        assert probe["merged_into_main"] is True, "deploy head SHA is not contained in main"
        assert set(probe["fragment_paths"]) >= EXPECTED_ENV_CONTRACT_FRAGMENTS

    async def test_deploy_run_outcome_success(self, pipeline):
        """The deploy run this mega triggered must conclude deploy_outcome=success.

        A running application only proves some container answers on the port;
        the typed outcome is what the pipeline itself concluded about the deploy.
        """
        if pipeline.get("task_status") != TaskStatus.DONE:
            pytest.skip("engineering failed")
        assert pipeline.get("deploy_run_error") is None, pipeline["deploy_run_error"]
        assert pipeline.get("deploy_outcome_error") is None, pipeline["deploy_outcome_error"]
        assert pipeline.get("deploy_outcome") == DeployOutcome.SUCCESS.value, (
            f"Deploy run {pipeline.get('deploy_run_id')} ended "
            f"deploy_outcome={pipeline.get('deploy_outcome')} "
            f"({pipeline.get('deploy_error_details')})"
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
        assert "deployed_url" in pipeline, "No deployed_url, port allocation missing?"

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
        if (
            pipeline.get("final_app_status") != ApplicationStatus.RUNNING.value
            or pipeline.get("deploy_outcome") != DeployOutcome.SUCCESS.value
        ):
            pytest.skip("deploy failed")
        assert pipeline.get("qa_result") == {
            "run_id": pipeline["qa_result"]["run_id"],
            "status": "completed",
            "qa_outcome": "passed",
        }


class TestFullPipelineLLM:
    """THE MEGA TEST with a real developer worker."""

    async def test_project_active(self, llm_pipeline):
        """Project status should be 'active' after successful scaffold + deploy."""
        assert llm_pipeline.get("agent_type") == "claude"
        assert llm_pipeline.get("scaffold_status") == ProjectStatus.ACTIVE, (
            f"Scaffold failed, status: {llm_pipeline.get('scaffold_status')}"
        )
        assert llm_pipeline.get("task_status") == TaskStatus.DONE, (
            f"Engineering failed, task status: {llm_pipeline.get('task_status')}"
        )
        assert llm_pipeline.get("final_app_status") == ApplicationStatus.RUNNING.value, (
            f"Deploy failed, app_status: {llm_pipeline.get('final_app_status')}"
        )

    async def test_no_user_secrets_required(self, llm_pipeline):
        """The backend-only LLM project must not trip the user-secret deploy path.

        Only *required* user secrets dead-end the deploy (DeployOutcome
        WAITING_FOR_USER_SECRET). Optional ``user_secret`` overrides such as the
        template's ``DATABASE_URL`` (``required: false``) are resolved from the
        allocated infrastructure and must not fail this project.
        """
        if llm_pipeline.get("task_status") != TaskStatus.DONE:
            pytest.skip("engineering failed")
        errors = llm_pipeline.get("env_contract_errors") or {}
        assert "merged" not in errors, errors.get("merged")
        probe = llm_pipeline["env_contract_probes"]["merged"]
        assert probe["required_user_secret_entries"] == [], (
            f"required user secrets would dead-end deploy: {probe['required_user_secret_entries']}"
        )

    async def test_deploy_run_outcome_success(self, llm_pipeline):
        """The deploy run this mega triggered must conclude deploy_outcome=success."""
        if llm_pipeline.get("task_status") != TaskStatus.DONE:
            pytest.skip("engineering failed")
        assert llm_pipeline.get("deploy_run_error") is None, llm_pipeline["deploy_run_error"]
        assert llm_pipeline.get("deploy_outcome_error") is None, llm_pipeline[
            "deploy_outcome_error"
        ]
        assert llm_pipeline.get("deploy_outcome") == DeployOutcome.SUCCESS.value, (
            f"Deploy run {llm_pipeline.get('deploy_run_id')} ended "
            f"deploy_outcome={llm_pipeline.get('deploy_outcome')} "
            f"({llm_pipeline.get('deploy_error_details')})"
        )

    async def test_health_endpoint(self, llm_pipeline):
        """GET /health on deployed service returns 200."""
        if llm_pipeline.get("final_app_status") != ApplicationStatus.RUNNING.value:
            pytest.skip("deploy failed")
        assert "deployed_url" in llm_pipeline, "No deployed_url, port allocation missing?"

        url = llm_pipeline["deployed_url"]
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

    async def test_non_llm_qa_passed(self, llm_pipeline):
        """A separate post-deploy QA run must terminate as passed."""
        if (
            llm_pipeline.get("final_app_status") != ApplicationStatus.RUNNING.value
            or llm_pipeline.get("deploy_outcome") != DeployOutcome.SUCCESS.value
        ):
            pytest.skip("deploy failed")
        assert llm_pipeline.get("qa_result") == {
            "run_id": llm_pipeline["qa_result"]["run_id"],
            "status": "completed",
            "qa_outcome": "passed",
        }
