"""No-model planning harness for an explicit catalog selection and confirmed brief."""

from .agents.architect.tools import plan_install, record_requirement_coverage, reset_task_chain
from .clients.api import api_client
from .kit_catalog import KitCatalog, get_kit_catalog_reader


async def scripted_install_plan(
    project_id: str, story_id: str, package: str, requirement_ids: list[str]
):
    catalog = await get_kit_catalog_reader().read()
    if not isinstance(catalog, KitCatalog):
        return {"error": "catalog_unavailable"}
    brief = await api_client.get_product_brief_by_story(story_id)
    if brief is None or str(brief.project_id) != project_id:
        return {"error": "confirmed_brief_required"}
    if set(requirement_ids) != {item.id for item in brief.content.must_requirements}:
        return {"error": "explicit_requirement_selection_required"}
    claim = await api_client.claim_planning_attempt(brief.id)
    if claim.outcome != "claimed":
        return {"error": f"planning_{claim.outcome}"}
    attempt = claim.planning_attempt_id
    try:
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
    admitted = await api_client.admit_product_brief_coverage(brief.id, attempt)
    return {
        "task_id": result["id"],
        "type": result["type"],
        "install": result["install"],
        "coverage_outcome": admitted.outcome,
    }
