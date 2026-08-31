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

import os

from live_harness import cleanup_guard
from pipeline_helpers import (
    DEPLOY_OUTCOME_TIMEOUT,
    DEPLOY_RUN_TIMEOUT,
    DEPLOY_TIMEOUT,
    ENGINEERING_TIMEOUT,
    EXPECTED_ENV_CONTRACT_FRAGMENTS,
    LLM_ENGINEERING_TIMEOUT,
    QA_RUN_TIMEOUT,
    SCAFFOLD_TIMEOUT,
    api_client_as_internal_service,
    api_client_as_test_user,
    api_client_as_unscoped_observer,
    cleanup_all,
    configured_qa_executor,
    create_llm_backend_project,
    create_noop_project,
    create_story_and_task,
    dump_debug,
    ensure_test_user,
    evidence_pass,
    po_input_cursor,
    probe_health_endpoint,
    record_env_contract,
    record_noop_settlement_evidence,
    request_undeploy,
    run_non_llm_qa,
    trigger_scaffold,
    verify_linear_noop_story_completion,
    verify_undeploy_residue,
    wait_application_not_deployed,
    wait_deploy,
    wait_deploy_outcome,
    wait_deploy_run,
    wait_engineering,
    wait_linear_noop_engineering,
    wait_owner_completion_notification,
    wait_scaffold,
    wait_service_deployment,
    wait_story_completed,
    wait_undeploy_run,
)
import pytest
import pytest_asyncio
from run_evidence import RunEvidenceCollector, emit_run_evidence

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _pipeline_run(
    create_project, *, engineering_timeout: int, debug_prefix: str, lifecycle_undeploy: bool = False
):
    """Full pipeline: scaffold → engineering → deploy. Yields context for assertions."""
    async with api_client_as_test_user() as api:
        # Deploy runs belong to no user, and list_runs hides unowned runs from the
        # non-admin harness user, so they are observed through a client that
        # authenticates only as an internal service and names no user.
        async with (
            api_client_as_unscoped_observer() as api_observer,
            api_client_as_internal_service() as api_internal,
        ):
            # The fixture user is registered by the service, then touched as
            # itself: registration is promo-gated for a named actor.
            await ensure_test_user(api, api_internal)
            ctx = await create_project(api, api_internal)
            async with cleanup_guard(
                lambda: cleanup_all(api_internal, api_observer, ctx), manifest=ctx["manifest"]
            ):
                # One artifact per combination, written before teardown removes
                # the containers it is collected from. The collector needs one
                # fact: this run's id — the same identity the project was
                # created with, which every worker this run causes carries as
                # `com.codegen.run.id` from the moment it exists.
                ctx["qa_agent_type_requested"] = os.getenv("LIVE_QA_AGENT_TYPE")
                ctx["run_evidence"] = RunEvidenceCollector(
                    run_id=ctx["manifest"].run_id,
                    # The second source, for the one case a label query cannot
                    # answer: a container that was removed rather than killed.
                    owned_workers=lambda: [
                        resource.identifier
                        for resource in ctx["manifest"].resources
                        if resource.kind == "worker"
                    ],
                )
                try:
                    async for value in _pipeline_phases(
                        api,
                        api_internal,
                        api_observer,
                        ctx,
                        engineering_timeout=engineering_timeout,
                        debug_prefix=debug_prefix,
                        lifecycle_undeploy=lifecycle_undeploy,
                    ):
                        yield value
                finally:
                    # Always ahead of cleanup_all, which is what removes the
                    # containers — and removal, not death, is what ends the
                    # readability of a labelled worker.
                    evidence_pass(ctx)
                    emit_run_evidence(ctx)


async def _complete_noop_lifecycle(api, api_internal, ctx: dict, *, debug_prefix: str) -> bool:
    """Complete the free mega lifecycle before its fixture exposes any facts."""
    if ctx.get("qa_result", {}).get("qa_outcome") != "passed":
        dump_debug(ctx, f"{debug_prefix}-qa")
        return False
    if await wait_story_completed(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-story-completed")
        return False
    if await wait_owner_completion_notification(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-owner-notification")
        return False
    if await wait_service_deployment(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-service-deployment")
        return False

    response = await api.get(f"/api/applications/{ctx['application_id']}")
    response.raise_for_status()
    ctx["application_before_undeploy"] = response.json()
    if ctx["application_before_undeploy"].get("status") != ApplicationStatus.RUNNING.value:
        ctx["application_before_undeploy_error"] = (
            f"application {ctx['application_id']} was not running before undeploy: "
            f"{ctx['application_before_undeploy'].get('status')}"
        )
        dump_debug(ctx, f"{debug_prefix}-pre-undeploy")
        return False
    await request_undeploy(api, api_internal, ctx)
    if ctx.get("undeploy_request_error"):
        dump_debug(ctx, f"{debug_prefix}-undeploy-request")
        return False
    if await wait_undeploy_run(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-undeploy-run")
        return False
    if await wait_application_not_deployed(api, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-not-deployed")
        return False
    if await verify_undeploy_residue(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-undeploy-residue")
        return False
    return True


async def _pipeline_phases(
    api,
    api_internal,
    api_observer,
    ctx: dict,
    *,
    engineering_timeout: int,
    debug_prefix: str,
    lifecycle_undeploy: bool,
):
    """The pipeline phases themselves, so evidence can wrap every exit from them."""
    if ctx.get("qa_requires_executor"):
        ctx["qa_agent_type"] = configured_qa_executor()

    # Phase 1: Scaffold
    trigger_scaffold(ctx)
    await wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT)
    if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
        yield ctx
        dump_debug(ctx, f"{debug_prefix}-scaffold")
        return

    # Phase 2: Engineering. Every poll takes an evidence pass: a retry removes
    # the previous attempt's container, and the attempt that died is exactly the
    # one that has to stay attributable.
    if lifecycle_undeploy:
        # A cursor fences out historical and foreign PO events.  It is captured
        # before the story can produce a completion notification.
        ctx["po_input_cursor"] = po_input_cursor()
    await create_story_and_task(api, ctx, linear_noop_tasks=lifecycle_undeploy)
    if lifecycle_undeploy:
        await wait_linear_noop_engineering(
            api,
            api_internal,
            ctx,
            timeout=engineering_timeout,
            on_poll=lambda: evidence_pass(ctx),
        )
        if ctx.get("task_status") == TaskStatus.DONE:
            await record_noop_settlement_evidence(api_internal, ctx)
            if ctx.get("noop_settlement_error") is not None:
                yield ctx
                dump_debug(ctx, f"{debug_prefix}-noop-settlement")
                return
            if not await verify_linear_noop_story_completion(api, ctx):
                yield ctx
                dump_debug(ctx, f"{debug_prefix}-noop-linear-story")
                return
    else:
        await wait_engineering(
            api, ctx, timeout=engineering_timeout, on_poll=lambda: evidence_pass(ctx)
        )
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

    await wait_deploy(api, api_observer, ctx, timeout=DEPLOY_TIMEOUT)
    await wait_deploy_outcome(api_internal, ctx, timeout=DEPLOY_OUTCOME_TIMEOUT)
    if (
        ctx.get("final_app_status") == ApplicationStatus.RUNNING.value
        and ctx.get("deploy_outcome") == DeployOutcome.SUCCESS.value
    ):
        # The external probe happens while the application is running, but its
        # evidence remains available after the noop lifecycle undeploys it.
        ctx["health_probe_before_undeploy"] = await probe_health_endpoint(ctx["deployed_url"])
        # The QA run is recorded before it is judged, so a QA cell can say
        # "exercised and failed" instead of falling back to "not exercised".
        # Every poll takes an evidence pass too: the QA executor's container is
        # removed as soon as the executor call returns, so this wait is the
        # window its exit code and log tail are still readable in.
        ctx["qa_result"] = await run_non_llm_qa(
            api_internal,
            ctx["story_id"],
            timeout=QA_RUN_TIMEOUT,
            record=lambda run: ctx.__setitem__("qa_run", run),
            on_poll=lambda: evidence_pass(ctx),
        )

    if lifecycle_undeploy and not await _complete_noop_lifecycle(
        api, api_internal, ctx, debug_prefix=debug_prefix
    ):
        yield ctx
        return

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
        lifecycle_undeploy=True,
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

    async def test_noop_paid_admission_and_settlement_are_durable(self, pipeline):
        """Every deterministic engineering attempt retains its paid-work evidence."""
        assert pipeline.get("noop_settlement_error") is None, pipeline.get("noop_settlement_error")
        settlement = pipeline.get("noop_settlement") or {}
        assert len(settlement) == 2
        for run_id, evidence in settlement.items():
            assert evidence["decision"]["agent_type"] == "noop", run_id
            assert evidence["decision"]["source"] == "project_pin", run_id
            assert evidence["admission"]["outcome"] == "admitted", run_id
            assert evidence["ledger"]["cost_source"] == "unknown", run_id
            assert evidence["ledger"]["cost_microusd"] is None, run_id

    async def test_two_noop_tasks_are_sequenced_reused_and_complete_before_deploy(self, pipeline):
        """A blocked second Task cannot run early or create another Story worker."""
        assert pipeline.get("noop_task_sequence_error") is None, pipeline.get(
            "noop_task_sequence_error"
        )
        assert pipeline.get("first_task_status") == TaskStatus.DONE
        assert pipeline.get("second_task_status") == TaskStatus.DONE
        assert pipeline.get("linear_noop_completion_error") is None, pipeline.get(
            "linear_noop_completion_error"
        )
        assert set(pipeline.get("linear_noop_task_statuses_before_deploy", {}).values()) == {
            TaskStatus.DONE
        }
        assert len(pipeline.get("linear_noop_worker_ids", [])) == 1

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

    async def test_health_endpoint(self, pipeline):
        """The externally reachable address answered before the lifecycle teardown."""
        probe = pipeline.get("health_probe_before_undeploy")
        assert probe, "No pre-undeploy health probe was recorded"
        assert probe["url"] == pipeline["deployed_url"]
        assert probe["status_code"] == 200, probe

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

    async def test_story_completed_and_owner_notification_delivered(self, pipeline):
        """QA completion leaves one durable completion record accepted by PO."""
        assert pipeline.get("story_terminal_error") is None, pipeline.get("story_terminal_error")
        assert pipeline.get("story_terminal", {}).get("status") == StoryStatus.COMPLETED.value
        notification = pipeline.get("owner_notification")
        event = pipeline.get("owner_notification_po_event")
        assert pipeline.get("owner_notification_error") is None, pipeline.get(
            "owner_notification_error"
        )
        assert notification and event
        assert notification["event"] == event["event"] == "story_completed"
        assert notification["project_id"] == event["project_id"] == pipeline["project_id"]
        assert notification["story_id"] == event["story_id"] == pipeline["story_id"]
        assert notification["terminal_status"] == StoryStatus.COMPLETED.value
        assert notification["task_id"] is None
        assert event["task_id"] == pipeline["story_id"]
        assert notification["text"] == event["text"]
        assert pipeline["deployed_url"] in notification["text"]

    async def test_deployment_sha_and_product_undeploy_lifecycle(self, pipeline):
        """The selected deployment matches merged SHA, then product undeploy clears it."""
        deployment = pipeline.get("service_deployment")
        assert pipeline.get("service_deployment_error") is None, pipeline.get(
            "service_deployment_error"
        )
        assert deployment and deployment["result"] == "success"
        assert deployment["deployed_sha"] == pipeline["deploy_head_sha"]
        assert pipeline.get("application_before_undeploy_error") is None
        assert (
            pipeline.get("application_before_undeploy", {}).get("status")
            == ApplicationStatus.RUNNING.value
        )
        assert pipeline.get("undeploy_request_error") is None, pipeline.get(
            "undeploy_request_error"
        )
        assert pipeline.get("undeploy_run_error") is None, pipeline.get("undeploy_run_error")
        assert pipeline.get("undeploy_run", {}).get("status") == "completed"
        assert pipeline.get("application_after_undeploy_error") is None
        assert (
            pipeline.get("application_after_undeploy", {}).get("status")
            == ApplicationStatus.NOT_DEPLOYED.value
        )
        assert pipeline.get("undeploy_residue_error") is None, pipeline.get(
            "undeploy_residue_error"
        )
        assert pipeline.get("undeploy_residue", {}).get("port_allocation_absent") is True


class TestFullPipelineLLM:
    """THE MEGA TEST with a real developer worker."""

    async def test_project_active(self, llm_pipeline):
        """Project status should be 'active' after successful scaffold + deploy."""
        assert llm_pipeline.get("agent_type") == os.getenv("LIVE_WORKER_AGENT_TYPE", "claude")
        assert llm_pipeline.get("scaffold_status") == ProjectStatus.ACTIVE, (
            f"Scaffold failed, status: {llm_pipeline.get('scaffold_status')}"
        )
        assert llm_pipeline.get("task_status") == TaskStatus.DONE, (
            f"Engineering failed, task status: {llm_pipeline.get('task_status')}"
        )
        assert llm_pipeline.get("final_app_status") == ApplicationStatus.RUNNING.value, (
            f"Deploy failed, app_status: {llm_pipeline.get('final_app_status')}"
        )

    async def test_requested_qa_executor_is_active(self, llm_pipeline):
        if not llm_pipeline.get("qa_requires_executor"):
            pytest.skip("ordinary mega uses deterministic health-only QA")
        assert llm_pipeline.get("qa_agent_type") == os.environ["LIVE_QA_AGENT_TYPE"]

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
        """GET /health evidence is recorded while the LLM deployment runs."""
        probe = llm_pipeline.get("health_probe_before_undeploy")
        assert probe, "No health probe was recorded"
        assert probe["status_code"] == 200, probe

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
