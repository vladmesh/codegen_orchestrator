"""No-model planning harness for an explicit catalog selection and confirmed brief."""

import argparse
import asyncio

import structlog
import yaml

from shared.contracts.dto.story_planning import PlanningChannels
from shared.log_config import setup_logging

from .agents.architect.tools import plan_install, record_requirement_coverage, reset_task_chain
from .clients.api import api_client
from .kit_catalog import KitCatalog, get_kit_catalog_reader


def select_capability(catalog: KitCatalog, capability: str) -> str:
    candidates = [item for item in catalog.packages if capability in item.package.capabilities]
    if len(candidates) != 1:
        raise ValueError("exactly one catalog capability match is required")
    selected = candidates[0]
    manifest = yaml.safe_load(catalog.manifests[selected.name])
    sources = {entry["source"]["kind"] for entry in manifest["environment"] if "source" in entry}
    if not {"platform_key", "platform_base_url"} <= sources:
        raise ValueError("catalog capability must declare platform sources")
    return selected.name


async def scripted_install_plan(
    project_id: str,
    story_id: str,
    package: str,
    requirement_ids: list[str],
    *,
    planning_attempt_id: str | None = None,
    capability: str | None = None,
):
    brief = await api_client.get_product_brief_by_story(story_id)
    if brief is None or str(brief.project_id) != project_id or not brief.confirmed_at:
        return {"error": "confirmed_brief_required"}
    if set(requirement_ids) != {item.id for item in brief.content.must_requirements}:
        return {"error": "explicit_requirement_selection_required"}
    if planning_attempt_id is not None:
        if not brief.planning_attempt_active or brief.planning_attempt_id != planning_attempt_id:
            return {"error": "planning_claim_not_owned"}
        attempt = planning_attempt_id
    else:
        claim = await api_client.claim_planning_attempt(brief.id)
        if claim.outcome != "claimed":
            return {"error": f"planning_{claim.outcome}"}
        attempt = claim.planning_attempt_id
    try:
        catalog = await get_kit_catalog_reader().read()
        if not isinstance(catalog, KitCatalog):
            return {"error": "catalog_unavailable"}
        if capability is not None:
            package = select_capability(catalog, capability)
        return await _owned_plan(
            catalog, brief, attempt, project_id, story_id, package, requirement_ids
        )
    finally:
        reset_task_chain()
        await api_client.finish_planning_attempt(brief.id, attempt)


async def _owned_plan(catalog, brief, attempt, project_id, story_id, package, requirement_ids):
    story = await api_client.get_story(story_id)
    if story.status == "created":
        await api_client.transition_story(story_id, "start")
    elif story.status != "in_progress":
        return {"error": "story_not_plannable"}
    reset_task_chain()
    result = await plan_install.coroutine(
        name=package,
        story_id=story_id,
        project_id=project_id,
        planning_attempt_id=attempt,
        kit_install_snapshot={
            "catalog": catalog.raw,
            "bindings": catalog.bindings,
            "manifests": catalog.manifests,
            "source": catalog.source,
            "core_version": catalog.core_version,
        },
    )
    if "error" in result:
        return result
    for requirement_id in requirement_ids:
        coverage = await record_requirement_coverage.coroutine(
            requirement_id=requirement_id,
            task_id=result["id"],
            brief_id=brief.id,
            planning_attempt_id=attempt,
        )
        if "error" in coverage:
            return coverage
    admitted = await api_client.admit_product_brief_coverage(
        brief.id, attempt, channels=PlanningChannels()
    )
    return {
        "task_id": result["id"],
        "type": result["type"],
        "install": result["install"],
        "coverage_outcome": admitted.outcome,
    }


async def _invoke(args):
    try:
        result = await scripted_install_plan(
            args.project,
            args.story,
            args.package,
            args.requirement,
            planning_attempt_id=args.attempt,
            capability=args.capability,
        )
        structlog.get_logger().info("scripted_install_result", result=result)
        return 0 if result.get("coverage_outcome") == "admitted" else 1
    finally:
        await api_client.close()


def main():
    """Fixed internal invocation in the LangGraph service, never a payload/command bridge."""
    setup_logging(service_name="scripted_install_plan", log_format="json")
    parser = argparse.ArgumentParser()
    for name in ("project", "story", "attempt"):
        parser.add_argument(f"--{name}", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--package")
    selection.add_argument("--capability")
    parser.add_argument("--requirement", action="append", required=True)
    return asyncio.run(_invoke(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
