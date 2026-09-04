"""Paid live proof of the confirmed Product Brief path.

This suite intentionally performs no model call from the PO.  It invokes the
released PO tools against the real API, then lets the real Architect, developer
and QA consumers perform their normal turns.  The only product-specific
behaviour is the brief's durable requirement data and the observable it asks
the generated service to expose.
"""

from __future__ import annotations

import os
import re
import uuid

from live_harness import OwnershipManifest, cleanup_guard
from pipeline_helpers import (
    API_URL,
    BRIEF_JOB_NAME,
    BRIEF_LANGUAGES,
    BRIEF_SETTINGS_KEY,
    DEPLOY_OUTCOME_TIMEOUT,
    DEPLOY_RUN_TIMEOUT,
    DEPLOY_TIMEOUT,
    LLM_ENGINEERING_TIMEOUT,
    ORCHESTRATOR_ROOT,
    QA_RUN_TIMEOUT,
    SCAFFOLD_TIMEOUT,
    api_client_as_internal_service,
    api_client_as_test_user,
    api_client_as_unscoped_observer,
    brief_detailed_spec,
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
    request_undeploy,
    run_brief_qa_and_retain_job_evidence,
    trigger_scaffold,
    verify_undeploy_residue,
    wait_application_not_deployed,
    wait_brief_engineering,
    wait_deploy,
    wait_deploy_outcome,
    wait_deploy_run,
    wait_product_brief_admission,
    wait_scaffold,
    wait_settings_seed_followup,
    wait_story_completed,
    wait_undeploy_run,
)
import pytest
import pytest_asyncio
from run_evidence import RunEvidenceCollector, emit_run_evidence

from shared.contracts.acceptance import parse_scheduled_behaviours
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome

pytestmark = pytest.mark.asyncio(loop_scope="module")

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


async def _po_create_confirmed_story(api, ctx: dict) -> None:
    """Use PO tools, not route-shaped test requests, for user intent."""
    config = _po_config(ctx["manifest"], ctx["project_id"])
    async with po_tool_boundary(api_url=API_URL) as po:
        presented = await po["present_product_brief"].ainvoke(
            {
                "project_id": ctx["project_id"],
                "title": "Multilingual scheduled digest",
                "summary": (
                    "A backend product that records one digest for every language selected "
                    "by its confirmed product setting."
                ),
                "must_requirements": [
                    {
                        "id": "scheduled_digest",
                        "text": "It runs a named scheduled digest behaviour on demand.",
                        "user_wording": "I need a scheduled digest I can test on demand.",
                    },
                    {
                        "id": "selected_languages",
                        "text": "One digest record is produced for every selected language.",
                        "user_wording": "The digest must be produced in Russian and English.",
                    },
                ],
                "initial_settings": [
                    {
                        "key": BRIEF_SETTINGS_KEY,
                        "scope": "product",
                        "value": BRIEF_LANGUAGES,
                    }
                ],
            },
            config=config,
        )
        match = _BRIEF_ID_RE.search(presented)
        assert match, f"PO did not present a Product Brief id: {presented}"
        ctx["brief_id"] = match.group(1)
        ctx["brief_requirement_ids"] = {"scheduled_digest", "selected_languages"}

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
                "title": "Build the multilingual scheduled digest",
                "description": brief_detailed_spec(),
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


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def product_brief_pipeline():  # noqa: PLR0911, PLR0915 - terminal phase evidence is explicit
    """PO tools → brief → Architect admission → engineering → deploy → QA."""
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
        }
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
                async with po_tool_boundary(api_url=API_URL) as po:
                    created = await po["create_project"].ainvoke(
                        {
                            "title": f"mega-brief-{uuid.uuid4().hex[:8]}",
                            "modules": "backend",
                            "description": brief_detailed_spec(),
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
                ctx["scaffold_task_description"] = brief_detailed_spec()

                trigger_scaffold(ctx)
                await wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT)
                if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
                    yield ctx
                    return

                ctx["po_input_cursor"] = po_input_cursor()
                await _po_create_confirmed_story(api, ctx)
                admitted = await wait_product_brief_admission(api, ctx)
                if admitted is None:
                    yield ctx
                    return
                repository = await api.get(f"/api/repositories/{ctx['repo_id']}")
                repository.raise_for_status()
                criteria = repository.json().get("acceptance_criteria") or ""
                behaviours = parse_scheduled_behaviours(criteria)
                if len(behaviours) != 1 or behaviours[0].name != BRIEF_JOB_NAME:
                    ctx["brief_acceptance_error"] = (
                        "Architect did not publish exactly the expected scheduled behaviour: "
                        f"{criteria!r}"
                    )
                    yield ctx
                    return
                if behaviours[0].arguments != {}:
                    ctx["brief_acceptance_error"] = (
                        f"Architect declared unexpected arguments for {BRIEF_JOB_NAME}: "
                        f"{behaviours[0].arguments}"
                    )
                    yield ctx
                    return
                ctx["brief_acceptance"] = {
                    "criterion": {
                        "name": behaviours[0].name,
                        "arguments": behaviours[0].arguments,
                        "observable": behaviours[0].observable,
                    }
                }

                await wait_brief_engineering(
                    api,
                    ctx,
                    timeout=LLM_ENGINEERING_TIMEOUT,
                    on_poll=lambda: evidence_pass(ctx),
                )
                if ctx.get("task_status") != TaskStatus.DONE:
                    yield ctx
                    return
                if not record_story_branch_ahead(ctx):
                    yield ctx
                    return

                if await wait_deploy_run(api_internal, ctx, timeout=DEPLOY_RUN_TIMEOUT) is None:
                    yield ctx
                    return
                await wait_deploy(api, api_observer, ctx, timeout=DEPLOY_TIMEOUT)
                deploy_result = await wait_deploy_outcome(
                    api_internal, ctx, timeout=DEPLOY_OUTCOME_TIMEOUT
                )
                if deploy_result is None:
                    yield ctx
                    return
                initial_deploy_run_id = ctx["deploy_run_id"]
                deploy_result = await wait_settings_seed_followup(
                    api_internal,
                    ctx,
                    deploy_result,
                    on_poll=lambda: evidence_pass(ctx),
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
                ctx["brief_settings_readback"] = await read_product_setting(
                    ctx, key=BRIEF_SETTINGS_KEY
                )
                ctx["qa_agent_type"] = configured_qa_executor()
                ctx["qa_result"] = await run_brief_qa_and_retain_job_evidence(
                    api_internal,
                    ctx,
                    job_name=BRIEF_JOB_NAME,
                    timeout=QA_RUN_TIMEOUT,
                    on_poll=lambda: evidence_pass(ctx),
                )
                # QA is scheduled by the normal QA consumer.  This helper only
                # waits for its terminal verdict; the capture proves an actual
                # central executor ran the Architect-owned criterion.
                evidence_pass(ctx)
                ctx["brief_qa_executor_executed"] = (
                    ctx["run_evidence"].executed_qa_agent().as_dict()
                )
                if await wait_story_completed(api_internal, ctx) is None:
                    yield ctx
                    return

                await request_undeploy(api, api_internal, ctx)
                if await wait_undeploy_run(api_internal, ctx) is None:
                    yield ctx
                    return
                if await wait_application_not_deployed(api, ctx) is None:
                    yield ctx
                    return
                await verify_undeploy_residue(api_internal, ctx)
                yield ctx
            finally:
                await record_terminal_stage_evidence(api_internal, ctx)
                evidence_pass(ctx)
                emit_run_evidence(ctx)


class TestProductBriefPipeline:
    """The one stand scenario that proves requirement data survives every stage."""

    async def test_confirmed_brief_was_covered_and_admitted(self, product_brief_pipeline):
        ctx = product_brief_pipeline
        assert ctx.get("scaffold_status") == ProjectStatus.ACTIVE
        assert ctx.get("brief_admission_error") is None, ctx.get("brief_admission_error")
        assert ctx["brief_read"]["confirmed_at"]
        assert ctx["brief_read"]["coverage_admitted_at"]
        assert {row["requirement_id"] for row in ctx["brief_coverage"]} == ctx[
            "brief_requirement_ids"
        ]
        assert all(row.get("task_id") for row in ctx["brief_coverage"])
        assert all(task["dispatch_admitted"] is True for task in ctx["brief_planned_tasks"])
        assert ctx.get("brief_acceptance_error") is None, ctx.get("brief_acceptance_error")
        assert ctx["brief_admission"]["released_task_ids"] == ctx["brief_plan_task_ids"]
        assert ctx["brief_acceptance"]["criterion"]["name"] == BRIEF_JOB_NAME
        assert ctx["brief_acceptance"]["criterion"]["arguments"] == {}

    async def test_engineering_deploy_and_settings_seed_succeeded(self, product_brief_pipeline):
        ctx = product_brief_pipeline
        assert ctx.get("brief_engineering_error") is None, ctx.get("brief_engineering_error")
        assert ctx.get("task_status") == TaskStatus.DONE
        assert ctx.get("story_branch_error") is None, ctx.get("story_branch_error")
        assert ctx.get("deploy_outcome") == DeployOutcome.SUCCESS.value, ctx.get(
            "deploy_error_details"
        )
        assert ctx.get("final_app_status") == ApplicationStatus.RUNNING.value
        assert ctx.get("deployed_image_error") is None, ctx.get("deployed_image_error")
        assert ctx["brief_settings_seed"] == [
            {
                "key": BRIEF_SETTINGS_KEY,
                "scope": "product",
                "subject_id": None,
                "written": True,
                "failure": None,
            }
        ]
        assert ctx["brief_settings_readback"] == {
            "contract_version": 1,
            "key": BRIEF_SETTINGS_KEY,
            "scope": "product",
            "subject_id": None,
            "value": BRIEF_LANGUAGES,
        }

    async def test_qa_fired_the_declared_job_and_the_story_cleaned_up(self, product_brief_pipeline):
        ctx = product_brief_pipeline
        assert ctx["qa_result"]["qa_outcome"] == "passed"
        executor = ctx["brief_qa_executor_executed"]
        assert executor["status"] == "captured", executor
        selected = ctx["qa_run_record"]["executor_decision"]["agent_type"]
        assert selected == ctx["qa_agent_type"] == executor["value"]
        if ctx.get("qa_agent_type_requested") is not None:
            assert ctx["qa_agent_type"] == ctx["qa_agent_type_requested"]
        evidence = ctx["brief_job_evidence"]
        assert evidence["command_id"] == f"qa-{ctx['qa_result']['run_id']}-{BRIEF_JOB_NAME}"
        assert evidence["name"] == BRIEF_JOB_NAME
        assert evidence["fired_by_product"] == ctx["project_id"]
        assert evidence["fired_by_run"] == ctx["qa_result"]["run_id"]
        assert evidence["dispatch_status"] == "dispatched"
        assert ctx.get("story_terminal", {}).get("status") == StoryStatus.COMPLETED.value
        assert ctx.get("undeploy_residue_error") is None, ctx.get("undeploy_residue_error")
        assert ctx.get("undeploy_residue", {}).get("port_allocation_absent") is True
