"""The Architect plans a product-local package source through the real create_task tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
import uuid

import httpx
import pydantic
import pytest

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.local_install import LocalPackageBuild
from src.catalog_install import INSTALL_PYTHON_VERSION
from src.kit_catalog import KitCatalogFailure, KitCatalogUnavailable
from tests.unit.factories import make_project, make_repository, make_story, make_task
from tests.unit.test_catalog_install import snapshot

PROJECT_ID = str(make_project().id)
#: The released names of the briefed catalog fixture.
RELEASED = ["tg-channels", "reminders"]


def _build(**overrides) -> dict:
    return {
        "path": "packages/channel-ranking",
        "package": {
            "name": "channel-ranking",
            "distribution": "codegen-kit-channel-ranking",
            "version": "0.1.0",
        },
        "core_version": CATALOG_ACTIVATION.core_version,
        "python_version": INSTALL_PYTHON_VERSION,
        "tooling_commit": CATALOG_ACTIVATION.tooling_commit,
        **overrides,
    }


@pytest.fixture
def api():
    with patch("src.agents.architect.tools.api_client") as api:
        api.get_project = AsyncMock(
            return_value=make_project(config={"modules": ["backend", "tg_bot"]})
        )
        api.get_primary_repository = AsyncMock(return_value=make_repository(id="repo-owned"))
        api.get_story = AsyncMock(return_value=make_story(id="story-abc"))
        api.create_task = AsyncMock(
            side_effect=[make_task(id="task-001"), make_task(id="task-002")]
        )
        yield api


@pytest.fixture(autouse=True)
def _fresh_chain():
    from src.agents.architect.tools import reset_task_chain

    reset_task_chain()
    yield
    reset_task_chain()


async def _create(build: dict | None = None, **overrides):
    from src.agents.architect.tools import create_task

    call = {
        "title": "Rank channel posts",
        "description": "Rank collected public-channel posts by configured signals.",
        "type": "feature",
        "acceptance_criteria": "Posts are returned in ranked order.",
        "story_id": "story-abc",
        "project_id": PROJECT_ID,
        "planning_attempt_id": "plan-1",
        "kit_catalog_packages": RELEASED,
        "kit_catalog_released": RELEASED,
        **overrides,
    }
    if build is not None:
        call["package_build"] = build
    return await create_task.ainvoke(call)


@pytest.mark.asyncio
async def test_a_build_is_one_owned_todo_feature_chained_after_the_previous_task(api):
    await _create(title="Install tg-channels prerequisite")

    result = await _create(_build())

    assert result["id"] == "task-002"
    payload = api.create_task.await_args_list[1].args[0]
    assert payload["type"] == "feature"
    assert payload["status"] == "todo"
    assert payload["repository_id"] == "repo-owned"
    assert payload["story_id"] == "story-abc"
    assert payload["project_id"] == PROJECT_ID
    assert payload["planning_attempt_id"] == "plan-1"
    assert payload["blocked_by_task_id"] == "task-001"
    assert payload["created_by"] == "architect"
    assert payload["install"] is None
    assert LocalPackageBuild.model_validate(payload["package_build"]) == (
        LocalPackageBuild.model_validate(_build())
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"core_version": "9.9.9"}, "core_version"),
        ({"python_version": "3.13.0"}, "python_version"),
        ({"tooling_commit": "f" * 40}, "tooling_commit"),
    ],
)
async def test_a_pin_other_than_the_activated_target_writes_nothing(api, override, field):
    result = await _create(_build(**override))

    assert f"package_build_pin_mismatch: {field}" in result["error"]
    api.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_host_off_the_activated_target_refuses_every_build(api, monkeypatch):
    monkeypatch.setattr("src.kit_catalog.installed_tooling_commit", lambda: "e" * 40)

    result = await _create(_build())

    assert "package_build_unavailable: catalog_inactive" in result["error"]
    api.create_task.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unowned",
    [
        "no_repository",
        "unmanaged_repository",
        "dependency_repository",
        "foreign_repository",
        "foreign_story",
        "missing_story",
        "draft_product",
        "bot_without_backend",
    ],
)
async def test_unavailable_ownership_refuses_before_any_write_or_chain_advance(api, unowned):
    other_product = uuid.uuid4()
    if unowned == "no_repository":
        api.get_primary_repository.return_value = None
    elif unowned == "unmanaged_repository":
        api.get_primary_repository.return_value = make_repository(is_managed=False)
    elif unowned == "dependency_repository":
        api.get_primary_repository.return_value = make_repository(role="dependency")
    elif unowned == "foreign_repository":
        api.get_primary_repository.return_value = make_repository(project_id=other_product)
    elif unowned == "foreign_story":
        # Same buyer, another of their products.
        api.get_story.return_value = make_story(project_id=other_product)
    elif unowned == "missing_story":
        request = httpx.Request("GET", "http://api/stories/story-abc")
        api.get_story.side_effect = httpx.HTTPStatusError(
            "not found", request=request, response=httpx.Response(404, request=request)
        )
    elif unowned == "draft_product":
        api.get_project.return_value = make_project(
            status="draft", config={"modules": ["backend", "tg_bot"]}
        )
    else:
        api.get_project.return_value = make_project(config={"modules": ["tg_bot"]})

    refused = await _create(_build())
    await _create(title="Ordinary follow-up")

    assert refused["error"].startswith("task 'Rank channel posts' was refused: package_build_")
    api.create_task.assert_awaited_once()
    assert api.create_task.await_args.args[0]["blocked_by_task_id"] is None


@pytest.mark.asyncio
async def test_a_build_on_another_task_type_is_refused_by_the_task_contract(api):
    result = await _create(_build(), type="fix")

    assert "a package build is a FEATURE" in result["error"]
    api.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_field_outside_the_build_intent_is_refused_at_the_tool_boundary(api):
    with pytest.raises(pydantic.ValidationError):
        await _create(_build(commit="a" * 40))

    api.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_build_cannot_target_the_wheel_store(api):
    result = await _create(_build(path="services/backend/packages/channel-ranking"))

    assert "package_build_path_refused" in result["error"]
    api.create_task.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("selectable", [RELEASED, []], ids=["selectable", "frozen"])
async def test_a_released_name_is_refused_whether_or_not_the_run_may_select(api, selectable):
    released = _build(
        package={"name": RELEASED[0], "distribution": "codegen-kit-ranking", "version": "0.2.0"}
    )

    refused = await _create(released, kit_catalog_packages=selectable)
    await _create(title="Ordinary follow-up", kit_catalog_packages=selectable)

    assert f"package_build_released: {RELEASED[0]}" in refused["error"]
    api.create_task.assert_awaited_once()
    assert api.create_task.await_args.args[0]["blocked_by_task_id"] is None


@pytest.mark.asyncio
async def test_unknown_released_names_admit_no_build_and_leave_ordinary_work(api):
    refused = await _create(_build(), kit_catalog_packages=None, kit_catalog_released=None)
    created = await _create(
        title="Ordinary follow-up", kit_catalog_packages=None, kit_catalog_released=None
    )

    assert "package_build_catalog_unavailable" in refused["error"]
    assert created["id"] == "task-001"
    api.create_task.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("Then run `kit add channel-ranking`.", "catalog does not list"),
        ("Install `kit add tg-channels` first.", "catalog_install_requires_plan_install"),
        ("Copy codegen_kit_ranking-0.1.0-py3-none-any.whl in.", "built artifact"),
    ],
)
async def test_catalog_and_artifact_prose_guards_still_refuse_with_a_build(
    api, description, reason
):
    result = await _create(_build(), description=description)

    assert reason in result["error"]
    api.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ordinary_task_never_acquires_a_build_from_its_words(api):
    await _create(
        title="Build a local package at packages/channel-ranking",
        description="Write package source for channel-ranking 0.1.0 under packages/.",
    )

    payload = api.create_task.await_args.args[0]
    assert "package_build" not in payload
    assert "repository_id" not in payload
    api.get_story.assert_not_awaited()


def test_the_briefing_names_the_activated_expectations_as_expectations():
    from src.consumers.architect import _local_package_briefing

    briefing = _local_package_briefing(snapshot())

    assert f"core_version {CATALOG_ACTIVATION.core_version}" in briefing
    assert f"python_version {INSTALL_PYTHON_VERSION}" in briefing
    assert f"tooling_commit {CATALOG_ACTIVATION.tooling_commit}" in briefing
    assert "not the product's observed compatibility" in briefing


@pytest.mark.parametrize("cause", ["host", "catalog"])
def test_without_the_target_or_the_catalog_the_briefing_offers_no_build(monkeypatch, cause):
    from src.consumers.architect import _local_package_briefing

    catalog = snapshot()
    if cause == "host":
        monkeypatch.setattr("src.kit_catalog.installed_tooling_commit", lambda: "e" * 40)
    else:
        catalog = KitCatalogUnavailable("test", KitCatalogFailure.TRANSPORT, "unreachable")

    briefing = _local_package_briefing(catalog)

    assert "unavailable this run" in briefing
    assert "Pass no package_build" in briefing
    assert "core_version" not in briefing
