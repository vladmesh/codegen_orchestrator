"""Paid live proof of the confirmed Product Brief path: the digest variant.

The flow these tests judge is `brief_pipeline.run_brief_pipeline`, shared by
every brief variant.  What is this suite's own is the product contract it runs
— the backend-only multilingual digest — and the expectations below, which are
stated for that contract and for no other.
"""

from __future__ import annotations

from brief_pipeline import run_brief_pipeline
from pipeline_helpers import (
    BRIEF_DIGEST_SCENARIO,
    BRIEF_JOB_NAME,
    BRIEF_LANGUAGES,
    BRIEF_SETTINGS_KEY,
)
import pytest
import pytest_asyncio

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def product_brief_pipeline():
    """PO tools → brief → Architect admission → engineering → deploy → QA."""
    async for ctx in run_brief_pipeline(BRIEF_DIGEST_SCENARIO):
        yield ctx


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
        ) or ctx.get("deploy_run_error")
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
