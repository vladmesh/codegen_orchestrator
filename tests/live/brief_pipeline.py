"""The confirmed Product Brief live flow, driven by the contract it is given.

This suite intentionally performs no model call from the PO.  It invokes the
released PO tools against the real API, then lets the real Architect, developer
and QA consumers perform their normal turns.  The only product-specific
behaviour is the brief's durable requirement data and the observable it asks
the generated service to expose — and that is exactly what a `BriefScenario`
carries, so a second variant of the path is a second contract rather than a
second copy of these stages.
"""

from __future__ import annotations

import os
import re
import uuid

from live_harness import OwnershipManifest, cleanup_guard
from pipeline_helpers import (
    API_URL,
    BRIEF_MAX_MANIFEST_REPAIRS,
    DEPLOY_OUTCOME_TIMEOUT,
    DEPLOY_RUN_TIMEOUT,
    DEPLOY_TIMEOUT,
    LLM_ENGINEERING_TIMEOUT,
    ORCHESTRATOR_ROOT,
    QA_RUN_TIMEOUT,
    SCAFFOLD_TIMEOUT,
    BriefScenario,
    api_client_as_internal_service,
    api_client_as_test_user,
    api_client_as_unscoped_observer,
    begin_brief_productive_window,
    brief_poll,
    cleanup_all,
    configured_qa_executor,
    ensure_test_user,
    evidence_pass,
    live_worker_agent_type,
    own_deploy_ahead,
    po_input_cursor,
    po_tool_boundary,
    read_product_setting,
    record_deployed_image_tags,
    record_story_branch_ahead,
    record_terminal_stage_evidence,
    report_brief_stage,
    request_undeploy,
    run_brief_qa_and_retain_job_evidence,
    trigger_scaffold,
    verify_undeploy_residue,
    wait_application_not_deployed,
    wait_brief_deploy_run,
    wait_brief_engineering,
    wait_deploy,
    wait_deploy_outcome,
    wait_product_brief_admission,
    wait_scaffold,
    wait_settings_seed_followup,
    wait_story_completed,
    wait_undeploy_run,
)
from run_evidence import RunEvidenceCollector, emit_run_evidence

from shared.contracts.acceptance import parse_scheduled_behaviours
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome

_PROJECT_ID_RE = re.compile(r"Project created\. ID: ([0-9a-f-]{36}),")
_BRIEF_ID_RE = re.compile(r"\(id: (brief-[a-f0-9]+)\)")
_STORY_ID_RE = re.compile(r"Story: (story-[A-Za-z0-9-]+) —")


def _po_config(manifest: OwnershipManifest, project_id: str) -> dict:
    return {
        "configurable": {
            "thread_id": manifest.run_id,
            "telegram_chat_id": "999000001",
            "project_creation_identity": {
                "project_id": project_id,
                "initiating_run_id": manifest.run_id,
            },
        }
    }


async def _po_create_confirmed_story(api, ctx: dict, scenario: BriefScenario) -> None:
    """Use PO tools, not route-shaped test requests, for user intent."""
    config = _po_config(ctx["manifest"], ctx["project_id"])
    async with po_tool_boundary(api_url=API_URL) as po:
        presented = await po["present_product_brief"].ainvoke(
            {
                "project_id": ctx["project_id"],
                "title": scenario.brief_title,
                "summary": scenario.brief_summary,
                "must_requirements": [dict(one) for one in scenario.must_requirements],
                "initial_settings": [
                    {
                        "key": scenario.settings_key,
                        "scope": "product",
                        "value": scenario.settings_value,
                    }
                ],
            },
            config=config,
        )
        match = _BRIEF_ID_RE.search(presented)
        assert match, f"PO did not present a Product Brief id: {presented}"
        ctx["brief_id"] = match.group(1)
        ctx["brief_requirement_ids"] = scenario.requirement_ids

        confirmed = await po["confirm_product_brief"].ainvoke(
            {"project_id": ctx["project_id"], "brief_id": ctx["brief_id"]}, config=config
        )
        assert "confirmed and frozen" in confirmed, confirmed

        # The deploy stack can arise immediately after the Architect's released
        # plan finishes, so recovery ownership precedes the story publication.
        own_deploy_ahead(ctx)
        created = await po["create_story"].ainvoke(
            {
                "project_id": ctx["project_id"],
                "title": scenario.story_title,
                "description": scenario.detailed_spec,
                "product_brief_id": ctx["brief_id"],
            },
            config=config,
        )
        match = _STORY_ID_RE.search(created)
        assert match, f"PO did not create and publish a story: {created}"
        ctx["story_id"] = match.group(1)

    # Read the frozen object over the API as a second fact; the rendered PO
    # message is an instruction to the user, not durable proof of confirmation.
    response = await api.get(f"/api/product-briefs/{ctx['brief_id']}")
    response.raise_for_status()
    ctx["brief_read"] = response.json()
    assert ctx["brief_read"].get("confirmed_at"), ctx["brief_read"]
    assert ctx["brief_read"].get("story_id") == ctx["story_id"]


async def run_brief_pipeline(  # noqa: C901, PLR0911, PLR0915 - every stage's exit is explicit
    scenario: BriefScenario,
):
    """PO tools → brief → Architect admission → engineering → deploy → QA.

    One flow, driven by the product contract it is given.  Every variant of the
    confirmed brief runs exactly these stages against exactly this machinery;
    what a variant owns is its contract and the behaviour shape it expects, and
    both arrive here as `scenario`.
    """
    async with (
        api_client_as_test_user() as api,
        api_client_as_internal_service() as api_internal,
        api_client_as_unscoped_observer() as api_observer,
    ):
        await ensure_test_user(api, api_internal)
        manifest = OwnershipManifest(run_id=f"live-{uuid.uuid4().hex[:12]}")
        project_id = str(uuid.uuid4())
        manifest.own("project", project_id)
        ctx = {
            "project_id": project_id,
            "manifest": manifest,
            "agent_type": live_worker_agent_type(),
            "modules": ["backend"],
            "qa_agent_type_requested": os.environ.get("LIVE_QA_AGENT_TYPE"),
            "qa_requires_executor": True,
            "brief_scenario": True,
            "brief_variant": scenario.name,
        }
        begin_brief_productive_window(ctx, productive_seconds=scenario.productive_seconds)
        manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{manifest.run_id}.json")
        async with cleanup_guard(
            lambda: cleanup_all(api_internal, api_observer, ctx), manifest=manifest
        ):
            ctx["run_evidence"] = RunEvidenceCollector(
                run_id=manifest.run_id,
                owned_workers=lambda: [
                    resource.identifier
                    for resource in manifest.resources
                    if resource.kind == "worker"
                ],
            )
            try:
                report_brief_stage(ctx, "scaffold", observed_state="project_creation")
                async with po_tool_boundary(api_url=API_URL) as po:
                    created = await po["create_project"].ainvoke(
                        {
                            "title": f"{scenario.project_prefix}-{uuid.uuid4().hex[:8]}",
                            "modules": "backend",
                            "description": scenario.detailed_spec,
                            "agent_type": ctx["agent_type"],
                        },
                        config=_po_config(manifest, project_id),
                    )
                match = _PROJECT_ID_RE.search(created)
                assert match and match.group(1) == project_id, created
                project_response = await api.get(f"/api/projects/{project_id}")
                project_response.raise_for_status()
                project = project_response.json()
                ctx["project_name"] = project["slug"]
                ctx["repo_name"] = project["slug"]
                repositories = await api.get(
                    "/api/repositories/", params={"project_id": project_id}
                )
                repositories.raise_for_status()
                assert len(repositories.json()) == 1
                ctx["repo_id"] = repositories.json()[0]["id"]
                manifest.own("repository", ctx["repo_id"], project_id=project_id)
                ctx["scaffold_task_description"] = scenario.detailed_spec

                report_brief_stage(ctx, "scaffold", observed_state="scaffold_requested")
                trigger_scaffold(ctx)
                await wait_scaffold(
                    api,
                    ctx,
                    timeout=SCAFFOLD_TIMEOUT,
                    on_poll=lambda: brief_poll(ctx, observed_state="scaffold_pending"),
                )
                if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
                    yield ctx
                    return

                report_brief_stage(ctx, "brief_admission", observed_state="scaffold_active")
                ctx["po_input_cursor"] = po_input_cursor()
                await _po_create_confirmed_story(api, ctx, scenario)
                admitted = await wait_product_brief_admission(
                    api,
                    ctx,
                    on_poll=lambda: brief_poll(ctx, observed_state="brief_admission_pending"),
                )
                if admitted is None:
                    yield ctx
                    return
                repository = await api.get(f"/api/repositories/{ctx['repo_id']}")
                repository.raise_for_status()
                criteria = repository.json().get("acceptance_criteria") or ""
                behaviours = parse_scheduled_behaviours(criteria)
                if len(behaviours) != 1 or behaviours[0].name != scenario.job_name:
                    ctx["brief_acceptance_error"] = (
                        "Architect did not publish exactly the expected scheduled behaviour: "
                        f"{criteria!r}"
                    )
                    yield ctx
                    return
                behaviour_error = scenario.behaviour_error(behaviours[0])
                if behaviour_error is not None:
                    ctx["brief_acceptance_error"] = behaviour_error
                    yield ctx
                    return
                ctx["brief_acceptance"] = {
                    "criterion": {
                        "name": behaviours[0].name,
                        "arguments": behaviours[0].arguments,
                        "observable": behaviours[0].observable,
                    }
                }

                report_brief_stage(ctx, "engineering", observed_state="tasks_released")
                await wait_brief_engineering(
                    api,
                    ctx,
                    api_internal=api_internal,
                    timeout=LLM_ENGINEERING_TIMEOUT,
                    on_poll=lambda: evidence_pass(ctx),
                )
                if ctx.get("task_status") != TaskStatus.DONE:
                    yield ctx
                    return
                if not record_story_branch_ahead(ctx):
                    yield ctx
                    return

                report_brief_stage(ctx, "generated_ci_deploy", observed_state="engineering_done")
                if (
                    await wait_brief_deploy_run(
                        api_internal,
                        ctx,
                        timeout=DEPLOY_RUN_TIMEOUT,
                        on_poll=lambda: brief_poll(ctx, observed_state="deploy_run_pending"),
                    )
                    is None
                ):
                    yield ctx
                    return
                await wait_deploy(
                    api,
                    api_observer,
                    ctx,
                    timeout=DEPLOY_TIMEOUT,
                    on_poll=lambda: brief_poll(ctx, observed_state="application_starting"),
                )
                deploy_result = await wait_deploy_outcome(
                    api_internal,
                    ctx,
                    timeout=DEPLOY_OUTCOME_TIMEOUT,
                    on_poll=lambda: brief_poll(ctx, observed_state="deploy_outcome_pending"),
                )
                if deploy_result is None:
                    yield ctx
                    return
                initial_deploy_run_id = ctx["deploy_run_id"]
                report_brief_stage(ctx, "settings_seed_repair", observed_state="initial_seed_typed")
                deploy_result = await wait_settings_seed_followup(
                    api_internal,
                    ctx,
                    deploy_result,
                    max_manifest_repairs=BRIEF_MAX_MANIFEST_REPAIRS,
                    on_poll=lambda: brief_poll(ctx, observed_state="settings_seed_followup"),
                )
                if deploy_result is None:
                    yield ctx
                    return
                if ctx["deploy_run_id"] != initial_deploy_run_id:
                    if deploy_result.deploy_outcome is not DeployOutcome.SUCCESS:
                        ctx["settings_seed_repair_error"] = (
                            f"fresh deploy Run {ctx['deploy_run_id']} reached typed outcome "
                            f"{deploy_result.deploy_outcome.value}, so it has no replacement "
                            "application"
                        )
                        yield ctx
                        return
                    if deploy_result.application_id is None:
                        ctx["settings_seed_repair_error"] = (
                            f"fresh successful deploy Run {ctx['deploy_run_id']} has no "
                            "application id"
                        )
                        yield ctx
                        return
                    await wait_deploy(
                        api,
                        api_observer,
                        ctx,
                        timeout=DEPLOY_TIMEOUT,
                        expected_application_id=deploy_result.application_id,
                        on_poll=lambda: brief_poll(ctx, observed_state="replacement_starting"),
                    )
                ctx["brief_settings_seed"] = [
                    seed.model_dump(mode="json") for seed in deploy_result.settings_seed
                ]
                if (
                    ctx.get("deploy_outcome") != DeployOutcome.SUCCESS.value
                    or ctx.get("final_app_status") != ApplicationStatus.RUNNING.value
                ):
                    yield ctx
                    return
                if not record_deployed_image_tags(ctx):
                    yield ctx
                    return
                if scenario.deployment_check is not None:
                    # What the deployed product had to be for this variant to
                    # prove anything, judged before a QA turn is spent on it and
                    # from the deployment's own artifacts. Every answer other
                    # than "it is" ends the run here with the reason.
                    report_brief_stage(
                        ctx, "deployment_shape", observed_state="application_running"
                    )
                    ctx["brief_deployment_error"] = scenario.deployment_check(ctx)
                    if ctx["brief_deployment_error"] is not None:
                        yield ctx
                        return
                ctx["brief_settings_readback"] = await read_product_setting(
                    ctx, key=scenario.settings_key
                )
                report_brief_stage(ctx, "qa", observed_state="settings_seeded")
                ctx["qa_agent_type"] = configured_qa_executor()
                ctx["qa_result"] = await run_brief_qa_and_retain_job_evidence(
                    api_internal,
                    ctx,
                    job_name=scenario.job_name,
                    timeout=QA_RUN_TIMEOUT,
                    on_poll=lambda: brief_poll(ctx, observed_state="qa_pending"),
                )
                # QA is scheduled by the normal QA consumer.  This helper only
                # waits for its terminal verdict; the capture proves an actual
                # central executor ran the Architect-owned criterion.
                evidence_pass(ctx)
                ctx["brief_qa_executor_executed"] = (
                    ctx["run_evidence"].executed_qa_agent().as_dict()
                )
                if (
                    await wait_story_completed(
                        api_internal,
                        ctx,
                        on_poll=lambda: brief_poll(ctx, observed_state="story_completion_pending"),
                    )
                    is None
                ):
                    yield ctx
                    return

                report_brief_stage(ctx, "undeploy", observed_state="qa_terminal")
                await request_undeploy(api, api_internal, ctx)
                if (
                    await wait_undeploy_run(
                        api_internal,
                        ctx,
                        on_poll=lambda: brief_poll(ctx, observed_state="undeploy_pending"),
                    )
                    is None
                ):
                    yield ctx
                    return
                if (
                    await wait_application_not_deployed(
                        api,
                        ctx,
                        on_poll=lambda: brief_poll(ctx, observed_state="application_stopping"),
                    )
                    is None
                ):
                    yield ctx
                    return
                await verify_undeploy_residue(api_internal, ctx)
                yield ctx
            finally:
                report_brief_stage(
                    ctx,
                    "teardown",
                    observed_state="fixture_finally",
                    enforce_deadline=False,
                )
                await record_terminal_stage_evidence(api_internal, ctx)
                evidence_pass(ctx)
                emit_run_evidence(ctx)
