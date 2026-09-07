"""Paid live proof of the confirmed Product Brief path: the package variant.

Same flow as the digest variant, same machinery, a different product contract:
a one-time reminder capability, which is the shape the architect's capability
ladder resolves to an in-process kit package.  So this run drives the package
route end to end — the architect plans a package, the engineering worker
installs it with the kit recipe, and central QA judges the package behaviour on
the route the criterion names.

The expectations below are this variant's own.  Its behaviour is
`reminders.tick`, whose arguments carry the `at` its declared schema requires —
not the argument-free behaviour the digest variant expects — and its observable
must be one central QA can bind to a read of the package's route, because a
package behaviour row passes on nothing else.
"""

from __future__ import annotations

from brief_pipeline import run_brief_pipeline
from pipeline_helpers import (
    BRIEF_PACKAGE_JOB_ARGUMENT,
    BRIEF_PACKAGE_JOB_NAME,
    BRIEF_PACKAGE_OWNER_REF,
    BRIEF_PACKAGE_REMINDER_STATE,
    BRIEF_PACKAGE_ROUTE,
    BRIEF_PACKAGE_SCENARIO,
    BRIEF_PACKAGE_SETTINGS_KEY,
)
import pytest
import pytest_asyncio

from services.langgraph.src.agents.qa.packages import observation_answers
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def product_brief_package_pipeline():
    """PO tools → brief → Architect admission → kit install → deploy → QA."""
    async for ctx in run_brief_pipeline(BRIEF_PACKAGE_SCENARIO):
        yield ctx


class TestProductBriefPackagePipeline:
    """The stand scenario that proves the brief path reaches the package route."""

    async def test_confirmed_brief_was_covered_and_admitted(self, product_brief_package_pipeline):
        ctx = product_brief_package_pipeline
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
        criterion = ctx["brief_acceptance"]["criterion"]
        assert criterion["name"] == BRIEF_PACKAGE_JOB_NAME
        assert BRIEF_PACKAGE_JOB_ARGUMENT in criterion["arguments"]

    async def test_the_published_observable_is_one_central_qa_can_bind(
        self, product_brief_package_pipeline
    ):
        """A package behaviour row passes only on a read of a route it names.

        Read here through the binder itself, so this asserts the same predicate
        the paid QA run will apply and not a paraphrase of it.
        """
        ctx = product_brief_package_pipeline
        observable = ctx["brief_acceptance"]["criterion"]["observable"]

        assert observation_answers(
            observable, "http_get", f"{BRIEF_PACKAGE_ROUTE}?user_ref={BRIEF_PACKAGE_OWNER_REF}"
        ), observable
        assert BRIEF_PACKAGE_REMINDER_STATE in observable
        assert not observation_answers(observable, "fire_job", BRIEF_PACKAGE_JOB_NAME)

    async def test_engineering_deploy_and_settings_seed_succeeded(
        self, product_brief_package_pipeline
    ):
        ctx = product_brief_package_pipeline
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
                "key": BRIEF_PACKAGE_SETTINGS_KEY,
                "scope": "product",
                "subject_id": None,
                "written": True,
                "failure": None,
            }
        ]
        assert ctx["brief_settings_readback"] == {
            "contract_version": 1,
            "key": BRIEF_PACKAGE_SETTINGS_KEY,
            "scope": "product",
            "subject_id": None,
            "value": BRIEF_PACKAGE_OWNER_REF,
        }

    async def test_qa_fired_the_package_behaviour_and_the_story_cleaned_up(
        self, product_brief_package_pipeline
    ):
        ctx = product_brief_package_pipeline
        assert ctx["qa_result"]["qa_outcome"] == "passed"
        executor = ctx["brief_qa_executor_executed"]
        assert executor["status"] == "captured", executor
        selected = ctx["qa_run_record"]["executor_decision"]["agent_type"]
        assert selected == ctx["qa_agent_type"] == executor["value"]
        if ctx.get("qa_agent_type_requested") is not None:
            assert ctx["qa_agent_type"] == ctx["qa_agent_type_requested"]
        evidence = ctx["brief_job_evidence"]
        assert evidence["command_id"] == f"qa-{ctx['qa_result']['run_id']}-{BRIEF_PACKAGE_JOB_NAME}"
        assert evidence["name"] == BRIEF_PACKAGE_JOB_NAME
        assert BRIEF_PACKAGE_JOB_ARGUMENT in evidence["arguments"]
        assert evidence["fired_by_product"] == ctx["project_id"]
        assert evidence["fired_by_run"] == ctx["qa_result"]["run_id"]
        assert evidence["dispatch_status"] == "dispatched"
        assert ctx.get("story_terminal", {}).get("status") == StoryStatus.COMPLETED.value
        assert ctx.get("undeploy_residue_error") is None, ctx.get("undeploy_residue_error")
        assert ctx.get("undeploy_residue", {}).get("port_allocation_absent") is True
