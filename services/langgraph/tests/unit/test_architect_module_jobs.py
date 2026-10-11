"""Selected package jobs are validated before the canonical checklist write."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from src.agents.architect import tools
from src.catalog_install import INSTALL_PYTHON_VERSION, plan_install_payload
from src.kit_catalog import install_snapshot
from src.planning_module_jobs import PlanningModuleFacts, module_job_contracts, plan_module_install
from tests.unit.factories import make_task


@pytest.fixture
def criteria_api(monkeypatch):
    api = AsyncMock()
    api.get_primary_repository.return_value = SimpleNamespace(id="repo-1")
    api.update_repository.side_effect = lambda repo, body: SimpleNamespace(id=repo, **body)
    monkeypatch.setattr(tools, "api_client", api)
    return api


@pytest.fixture
def selected_jobs(activated_kit_catalog):
    install = plan_install_payload(activated_kit_catalog, "tg-channels", INSTALL_PYTHON_VERSION)
    return module_job_contracts(activated_kit_catalog, [install])


def test_repeated_requirements_select_one_verified_job_contract(activated_kit_catalog):
    install = plan_install_payload(activated_kit_catalog, "tg-channels", INSTALL_PYTHON_VERSION)

    contracts = module_job_contracts(activated_kit_catalog, [install] * 16)

    [contract] = contracts.values()
    assert contract["package"] == install.package.model_dump(mode="json")
    assert contract["jobs"]["tg_channels.tick"]["required"] == ["at"]
    assert contract["timers"] == {"tg_channels.tick": 60}


def test_a_later_selection_of_the_same_package_cannot_hide_drift(activated_kit_catalog):
    from src.catalog_install import InstallRefusal

    install = plan_install_payload(activated_kit_catalog, "tg-channels", INSTALL_PYTHON_VERSION)
    drifted = install.model_copy(update={"catalog_digest": "0" * 64})

    with pytest.raises(InstallRefusal, match="plan_drift: tg-channels"):
        module_job_contracts(activated_kit_catalog, [install, drifted])


@pytest.mark.parametrize("name", ["tg-channels", "reminders"])
def test_planning_job_facts_match_the_pinned_native_registry_composition(
    activated_kit_catalog, name
):
    from framework.spec.loader import AllSpecs, _merge_package_names
    from framework.spec.package_resolution import ActivePackage

    from src.catalog_install import selected_package_manifest

    install = plan_install_payload(activated_kit_catalog, name, INSTALL_PYTHON_VERSION)
    [contract] = module_job_contracts(activated_kit_catalog, [install]).values()
    # The native merge reads no models/events; these placeholders keep the test data-only.
    specs = AllSpecs(models=None, events=None)
    package = ActivePackage(
        name=name,
        manifest=selected_package_manifest(activated_kit_catalog, name),
        package_root=Path("unused"),
        manifest_sha256=contract["manifest_sha256"],
    )
    errors = []
    _merge_package_names(specs, package, {}, {}, {}, {}, errors)
    assert errors == []
    assert contract["jobs"] == specs.job_schemas
    assert contract["timers"] == specs.job_timers
    assert set(specs.job_schema_sources.values()) == {f"package:{name}"}


@pytest.mark.asyncio
async def test_unknown_selected_module_job_is_refused_before_any_write(criteria_api, selected_jobs):
    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "acceptance_criteria": (
                "- FIRE JOB tg_channels.poll THEN GET /tg-channels/digest shows configured posts"
            ),
            "selected_module_jobs": selected_jobs,
        }
    )

    assert "tg_channels.poll" in result["error"]
    assert "tg_channels.tick" in result["error"]
    criteria_api.update_repository.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    ["", " WITH {}", ' WITH {"at":42}', ' WITH {"at":"2026-10-11T12:00:00Z","extra":true}'],
)
async def test_selected_module_arguments_use_the_native_schema(
    criteria_api, selected_jobs, arguments
):
    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "acceptance_criteria": (
                f"- FIRE JOB tg_channels.tick{arguments} THEN "
                "GET /tg-channels/digest shows configured posts"
            ),
            "selected_module_jobs": selected_jobs,
        }
    )
    assert "Invalid arguments for tg_channels.tick" in result["error"]
    criteria_api.update_repository.assert_not_awaited()


@pytest.mark.asyncio
async def test_declared_module_and_product_owned_jobs_are_written_unchanged(
    criteria_api, selected_jobs
):
    criteria = (
        '- FIRE JOB tg_channels.tick WITH {"at":"2026-10-11T12:00:00Z"} THEN '
        "GET /tg-channels/digest shows configured posts\n"
        '- FIRE JOB daily_digest WITH {"languages":["ru","en"]} THEN '
        "the owner sees configured languages\n"
        "- FIRE JOB another_module.tick THEN GET /output shows the result"
    )
    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "acceptance_criteria": criteria,
            "selected_module_jobs": selected_jobs,
        }
    )
    assert result["acceptance_criteria"] == criteria
    criteria_api.update_repository.assert_awaited_once_with(
        "repo-1", {"acceptance_criteria": criteria}
    )


@pytest.mark.asyncio
async def test_no_module_criteria_remain_supported(criteria_api):
    criteria = (
        "- GET /health returns 200\n- FIRE JOB service.sync THEN GET /output shows the result"
    )
    result = await tools.update_acceptance_criteria.ainvoke(
        {"project_id": "project-1", "acceptance_criteria": criteria, "selected_module_jobs": {}}
    )
    assert result["acceptance_criteria"] == criteria


@pytest.mark.asyncio
async def test_later_duplicate_lines_cannot_hide_invalid_module_arguments(
    criteria_api, selected_jobs
):
    criteria = (
        '- FIRE JOB tg_channels.tick WITH {"at":"2026-10-11T12:00:00Z"} THEN '
        "GET /digest shows posts\n"
        "- FIRE JOB tg_channels.tick THEN GET /digest shows posts"
    )
    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "acceptance_criteria": criteria,
            "selected_module_jobs": selected_jobs,
        }
    )
    assert "Invalid arguments" in result["error"]
    criteria_api.update_repository.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("job,accepted", [("poll", False), ("tick", True)])
async def test_explicit_install_uses_its_persisted_selected_release(
    criteria_api, activated_kit_catalog, job, accepted
):
    install = plan_install_payload(activated_kit_catalog, "tg-channels", INSTALL_PYTHON_VERSION)
    criteria_api.get_tasks_by_story.return_value = [make_task(type="install", install=install)]
    criteria = (
        f'- FIRE JOB tg_channels.{job} WITH {{"at":"2026-10-11T12:00:00Z"}} THEN '
        "GET /digest shows configured posts"
    )
    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "story_id": "story-1",
            "acceptance_criteria": criteria,
            "selected_module_jobs": {},
            "kit_install_snapshot": install_snapshot(activated_kit_catalog),
        }
    )
    criteria_api.get_tasks_by_story.assert_awaited_once_with("story-1")
    assert ("acceptance_criteria" in result) is accepted
    assert criteria_api.update_repository.await_count == int(accepted)


@pytest.mark.asyncio
async def test_explicit_install_contract_drift_refuses_the_criteria_write(
    criteria_api, activated_kit_catalog
):
    install = plan_install_payload(activated_kit_catalog, "tg-channels", INSTALL_PYTHON_VERSION)
    install = install.model_copy(update={"catalog_digest": "0" * 64})
    criteria_api.get_tasks_by_story.return_value = [make_task(type="install", install=install)]
    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "story_id": "story-1",
            "acceptance_criteria": "- GET /health returns 200",
            "selected_module_jobs": {},
            "kit_install_snapshot": install_snapshot(activated_kit_catalog),
        }
    )
    assert "plan_drift" in result["error"]
    criteria_api.update_repository.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["catalog", "binding", "manifest", "activation", "duplicate"])
async def test_explicit_criteria_rechecks_changed_inputs_before_writing(
    criteria_api, activated_kit_catalog, changed
):
    injected = deepcopy(install_snapshot(activated_kit_catalog))
    facts = PlanningModuleFacts()
    catalog = facts.restore(injected)
    install, _ = plan_module_install(catalog, "reminders", INSTALL_PYTHON_VERSION, facts=facts)
    tasks = [make_task(type="install", install=install)]
    if changed == "catalog":
        injected["catalog"] += "\n# changed\n"
    elif changed == "binding":
        injected["bindings"]["reminders"] = injected["bindings"]["reminders"].replace(
            "textparse.when", "invented.when"
        )
    elif changed == "manifest":
        injected["manifests"]["reminders"] = "jobs_schema: ["
    elif changed == "activation":
        injected["commit"] = "c" * 40
    else:
        tasks.append(
            make_task(
                type="install", install=install.model_copy(update={"catalog_digest": "0" * 64})
            )
        )
    criteria_api.get_tasks_by_story.return_value = tasks

    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "story_id": "story-1",
            "acceptance_criteria": "- GET /health returns 200",
            "selected_module_jobs": {},
            "kit_install_snapshot": injected,
        },
        config={"configurable": {"planning_module_facts": facts}},
    )
    assert "Selected module job contract refused" in result["error"]
    criteria_api.update_repository.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_historical_install_does_not_select_a_module(
    criteria_api, activated_kit_catalog
):
    install = plan_install_payload(activated_kit_catalog, "tg-channels", INSTALL_PYTHON_VERSION)
    drifted = install.model_copy(update={"catalog_digest": "0" * 64})
    criteria_api.get_tasks_by_story.return_value = [
        make_task(type="install", install=drifted, status="cancelled")
    ]
    criteria = "- FIRE JOB service.sync THEN GET /output shows posts"
    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "story_id": "story-1",
            "acceptance_criteria": criteria,
            "selected_module_jobs": {},
            "kit_install_snapshot": install_snapshot(activated_kit_catalog),
        }
    )
    assert result["acceptance_criteria"] == criteria


@pytest.mark.asyncio
async def test_selected_package_with_no_jobs_still_owns_its_namespace(
    criteria_api, activated_kit_catalog
):
    manifests = dict(activated_kit_catalog.manifests)
    manifest = yaml.safe_load(manifests["tg-channels"])
    manifest["jobs_schema"]["properties"] = {}
    manifest["timers"] = []
    manifests["tg-channels"] = yaml.safe_dump(manifest)
    catalog = replace(activated_kit_catalog, manifests=manifests)
    install = plan_install_payload(catalog, "tg-channels", INSTALL_PYTHON_VERSION)
    contracts = module_job_contracts(catalog, [install])
    result = await tools.update_acceptance_criteria.ainvoke(
        {
            "project_id": "project-1",
            "acceptance_criteria": "- FIRE JOB tg_channels.tick THEN GET /output shows posts",
            "selected_module_jobs": contracts,
        }
    )
    assert "declared names: (none)" in result["error"]
    criteria_api.update_repository.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("job,accepted", [("poll", False), ("tick", True)])
async def test_graph_injects_authoritative_contracts_despite_model_arguments(
    criteria_api, selected_jobs, job, accepted
):
    from langchain_core.messages import AIMessage

    from src.agents.architect.graph import create_architect_graph
    from tests.unit.test_architect_graph import _ScriptedToolCallingModel

    criteria = (
        f'- FIRE JOB tg_channels.{job} WITH {{"at":"2026-10-11T12:00:00Z"}} THEN '
        "GET /digest shows posts"
    )
    model = _ScriptedToolCallingModel(
        turns=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "update_acceptance_criteria",
                        "id": "criterion-write",
                        "args": {
                            "project_id": "project-1",
                            "acceptance_criteria": criteria,
                            "selected_module_jobs": {},
                        },
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    await create_architect_graph(model).ainvoke(
        {
            "messages": [{"role": "user", "content": "Write the module criterion"}],
            "project_id": "project-1",
            "story_id": "story-1",
            "selected_module_jobs": selected_jobs,
            "kit_install_snapshot": None,
        }
    )
    assert criteria_api.update_repository.await_count == int(accepted)
