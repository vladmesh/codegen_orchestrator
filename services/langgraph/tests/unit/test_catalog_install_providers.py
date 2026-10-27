"""The Architect plans a catalog INSTALL only after the packages that provide what it requires.

`plan_install` runs against the activated snapshot's genuine bytes, where ranking 0.1.0
requires `tg-channels.posts.v1` and tg-channels 0.2.0 provides it; only the API is in memory.
"""

from __future__ import annotations

import pytest

from shared.contracts.dto.catalog_install import CatalogInstall
from shared.contracts.dto.task import TaskStatus, TaskType
from src.agents.architect import tools
from src.catalog_install import INSTALL_PYTHON_VERSION, plan_install_payload
from src.kit_catalog import install_snapshot
from src.planning_module_jobs import PlanningModuleFacts
from tests.unit.factories import make_project, make_repository, make_task

# Its tests together cost seconds of CPU: CI runs this file, the host profile skips it.
pytestmark = pytest.mark.cpu_heavy

STORY_ID = "story-top-posts"
PROJECT_ID = "5b1d0c9e-0f6a-4c55-9a43-6f0f1a7e2b10"
#: One snapshot restore and closure resolution shared by every plan here.
FACTS = PlanningModuleFacts()
PROVIDER_MISSING = (
    "provider_missing: ranking requires tg-channels.posts.v1 (provided by tg-channels)"
)


class _Api:
    """The project, its repository and its tasks; the done INSTALL tasks are the product's."""

    def __init__(self, done=()):
        self.done = list(done)
        self.created: list[dict] = []

    async def get_project(self, project_id, **_kwargs):
        return make_project(config={"modules": ["backend", "tg_bot"]})

    async def get_primary_repository(self, project_id):
        return make_repository()

    async def get_tasks_by_project(self, project_id, *, task_type, status):
        return [task for task in self.done if (task.type, task.status) == (task_type, status)]

    async def create_task(self, task_data):
        self.created.append(task_data)
        return make_task(
            id=f"task-{len(self.created)}",
            type=task_data["type"],
            story_id=task_data["story_id"],
            install=task_data["install"],
            blocked_by_task_id=task_data["blocked_by_task_id"],
        )


@pytest.fixture
def api(monkeypatch):
    fake = _Api()
    monkeypatch.setattr(tools, "api_client", fake)
    tools.reset_task_chain()
    yield fake
    tools.reset_task_chain()


async def _plan(catalog, name: str) -> dict:
    return await tools.plan_install.coroutine(
        name=name,
        story_id=STORY_ID,
        project_id=PROJECT_ID,
        kit_install_snapshot=install_snapshot(catalog),
        config={"configurable": {"planning_module_facts": FACTS}},
    )


def _installed(install: CatalogInstall, *, status=TaskStatus.DONE):
    return make_task(
        id=f"installed-{install.package.name}",
        type=TaskType.INSTALL,
        status=status,
        story_id="story-earlier",
        install=install,
    )


def _channels(catalog) -> CatalogInstall:
    return plan_install_payload(catalog, "tg-channels", INSTALL_PYTHON_VERSION)


@pytest.mark.asyncio
async def test_a_package_without_its_provider_is_refused_by_name(api, activated_kit_catalog):
    result = await _plan(activated_kit_catalog, "ranking")

    assert result == {"error": PROVIDER_MISSING}
    assert api.created == []


@pytest.mark.asyncio
async def test_a_package_planned_before_its_provider_is_refused_the_same_way(
    api, activated_kit_catalog
):
    first = await _plan(activated_kit_catalog, "ranking")
    provider = await _plan(activated_kit_catalog, "tg-channels")

    assert first == {"error": PROVIDER_MISSING}
    assert [task["install"]["package"]["name"] for task in api.created] == ["tg-channels"]
    assert provider["id"] == "task-1"


@pytest.mark.asyncio
async def test_the_provider_then_its_dependent_is_one_ordered_chain(api, activated_kit_catalog):
    provider = await _plan(activated_kit_catalog, "tg-channels")
    dependent = await _plan(activated_kit_catalog, "ranking")

    assert [
        (task["install"]["package"]["name"], task["install"]["package"]["version"])
        for task in api.created
    ] == [("tg-channels", "0.2.0"), ("ranking", "0.1.0")]
    assert dependent["blocked_by_task_id"] == provider["id"]
    assert [task["install"]["binding"]["resource"] for task in api.created] == [
        "codegen_kit_tg_channels:bindings/default.yaml",
        "codegen_kit_ranking:bindings/default.yaml",
    ]


@pytest.mark.asyncio
async def test_a_provider_installed_in_the_product_admits_its_dependent(api, activated_kit_catalog):
    api.done = [_installed(_channels(activated_kit_catalog))]

    result = await _plan(activated_kit_catalog, "ranking")

    assert "error" not in result, result
    [task] = api.created
    assert task["install"] == plan_install_payload(
        activated_kit_catalog, "ranking", INSTALL_PYTHON_VERSION
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_an_unfinished_install_elsewhere_in_the_product_provides_nothing(
    api, activated_kit_catalog
):
    api.done = [_installed(_channels(activated_kit_catalog), status=TaskStatus.TODO)]

    assert await _plan(activated_kit_catalog, "ranking") == {"error": PROVIDER_MISSING}


@pytest.mark.asyncio
async def test_an_installed_release_that_provides_nothing_is_no_provider(
    api, activated_kit_catalog
):
    current = _channels(activated_kit_catalog)
    older = current.package.model_copy(
        update={"version": "0.1.2", "tag": "packages/tg-channels/v0.1.2"}
    )
    api.done = [_installed(current.model_copy(update={"package": older}))]

    assert await _plan(activated_kit_catalog, "ranking") == {"error": PROVIDER_MISSING}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["tg-channels", "reminders"])
async def test_a_package_that_requires_nothing_is_planned_alone(api, activated_kit_catalog, name):
    result = await _plan(activated_kit_catalog, name)

    assert "error" not in result, result
    [task] = api.created
    assert task["install"] == plan_install_payload(
        activated_kit_catalog, name, INSTALL_PYTHON_VERSION
    ).model_dump(mode="json")
    assert task["blocked_by_task_id"] is None
