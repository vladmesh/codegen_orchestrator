"""Deterministic closure uses the exact catalog and resource briefed to planning."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from framework.catalog import parse_catalog
import pytest

from shared.contracts.dto.project import ProjectDTO
from src.agents.architect import tools
from src.catalog_install import InstallRefusal, plan_install_payload
from src.kit_catalog import installable

DATA = Path(__file__).parent / "fixtures" / "catalog-install"


def snapshot():
    catalog = installable(parse_catalog((DATA / "catalog.yaml").read_text()), "test")
    return replace(
        catalog,
        bindings={"reminders": (DATA / "default.yaml").read_text()},
        manifests={"reminders": (DATA / "package.yaml").read_text()},
        raw=(DATA / "catalog.yaml").read_text(),
    )


@pytest.mark.asyncio
async def test_install_reads_product_modules_from_the_real_api_shape(monkeypatch):
    now = datetime.now(UTC)
    project = ProjectDTO.model_validate(
        {
            "id": str(uuid.uuid4()),
            "title": "Notes",
            "slug": "notes",
            "status": "active",
            "config": {"modules": ["backend", "tg_bot"]},
            "owner_id": 1,
            "created_at": now,
        }
    )
    api = AsyncMock()
    api.get_project.return_value = project
    api.get_primary_repository.return_value = SimpleNamespace(id="repo-notes")
    api.create_task.return_value = SimpleNamespace(
        id="task-install", model_dump=lambda **_: {"id": "task-install"}
    )
    monkeypatch.setattr(tools, "api_client", api)
    catalog = snapshot()
    tools.reset_task_chain()
    try:
        result = await tools.plan_install.coroutine(
            "reminders",
            "story-notes",
            str(project.id),
            {
                "catalog": (DATA / "catalog.yaml").read_text(),
                "bindings": catalog.bindings,
                "manifests": catalog.manifests,
                "source": catalog.source,
                "core_version": catalog.core_version,
            },
        )
        assert result == {"id": "task-install"}
        body = api.create_task.call_args.args[0]
        assert body["type"] == "install" and body["repository_id"] == "repo-notes"
        assert body["install"]["package"]["version"] == "0.5.0"
    finally:
        tools.reset_task_chain()


def test_closes_reminders_library_and_default_binding_as_one_payload():
    payload = plan_install_payload(snapshot(), "reminders", "3.12.0")
    assert payload.package.name == "reminders"
    assert payload.package.version == "0.5.0"
    assert [item.name for item in payload.libraries] == ["textparse"]
    assert payload.binding.functions == ["textparse.when"]
    assert payload.binding.package == payload.package.name


@pytest.mark.parametrize(
    "name,python,reason",
    [
        ("invented", "3.12.0", "unknown_package"),
        ("reminders", "3.10.0", "incompatible_library"),
    ],
)
def test_refuses_unknown_or_incompatible_before_creating_task(name, python, reason):
    with pytest.raises(InstallRefusal, match=reason):
        plan_install_payload(snapshot(), name, python)


def test_missing_required_binding_resource_is_a_named_refusal():
    with pytest.raises(InstallRefusal, match="binding_unavailable"):
        plan_install_payload(replace(snapshot(), bindings={}), "reminders", "3.12.0")


def test_binding_dependency_cannot_be_replaced_by_an_arbitrary_function():
    current = snapshot()
    corrupt = current.bindings["reminders"].replace("textparse.when", "invented.when")
    with pytest.raises(InstallRefusal, match="binding_dependency_missing"):
        plan_install_payload(
            replace(current, bindings={"reminders": corrupt}), "reminders", "3.12.0"
        )
