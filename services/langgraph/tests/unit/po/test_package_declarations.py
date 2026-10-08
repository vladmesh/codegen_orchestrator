"""Explicit package reliance leaves ordinary language/timezone settings independent."""

from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from src.agents.po.tools_briefs import confirm_product_brief
from src.catalog_product_settings import PO_CATALOG_CONFIG_KEY, render_po_packages
from tests.unit.po.catalog_packages import platform_and_v1_snapshot
from tests.unit.po.test_tools_briefs import (
    _API,
    BRIEF_ID,
    PROJECT_ID,
    _brief,
    _config,
    _install,
    _stored_content,
)


@pytest.fixture
def catalog():
    return platform_and_v1_snapshot()


async def confirm(catalog, declared, initial, *, russian=False):
    content = _stored_content(settings=initial)
    if russian:
        content["language"] = "ru"
        content["summary"] = "Бот присылает свежие публикации из выбранных каналов."
        content["must_requirements"][0].update(
            text="Получать новые посты из указанных каналов",
            user_wording="Хочу новости из этих двух каналов",
        )
    api = _API(briefs={BRIEF_ID: _brief(content=content)})
    _install(api, AsyncMock())
    config = _config()
    config["configurable"][PO_CATALOG_CONFIG_KEY] = catalog
    answer = await confirm_product_brief.ainvoke(
        {"project_id": PROJECT_ID, "brief_id": BRIEF_ID, "catalog_packages": declared},
        config=config,
    )
    return answer, api


@pytest.mark.parametrize("key,value", [("language", "ru"), ("timezone", "Europe/Nicosia")])
async def test_ordinary_brief_with_generic_binding_key_is_confirmed(catalog, key, value):
    current, _ = catalog
    answer, api = await confirm(current, [], [{"key": key, "value": value, "description": key}])
    assert "confirmed and frozen" in answer
    assert api.briefs[BRIEF_ID]["confirmed_at"]


async def test_declared_v2_without_language_is_refused_with_allowed_values(catalog):
    current, package = catalog
    answer, api = await confirm(current, [package], [])
    result = json.loads(answer)
    assert api.posts == []
    assert result["status"] == "package_settings_required"
    assert [(item["key"], item["schema"]["enum"]) for item in result["settings"]] == [
        ("language", ["ru", "en"])
    ]


async def test_declared_v2_with_language_and_no_seed_is_confirmed(catalog):
    current, package = catalog
    answer, api = await confirm(
        current, [package], [{"key": "language", "value": "en", "description": "English replies"}]
    )
    assert "confirmed and frozen" in answer
    assert api.briefs[BRIEF_ID]["confirmed_at"]


async def test_russian_channel_order_requires_language_but_not_seed(catalog):
    current, package = catalog
    answer, api = await confirm(
        current,
        [package],
        [
            {
                "key": "channels",
                "value": ["first_public", "second_public"],
                "description": "Мои каналы",
            }
        ],
        russian=True,
    )
    result = json.loads(answer)
    assert api.posts == []
    assert [item["key"] for item in result["settings"]] == ["language"]


async def test_unknown_declared_package_gets_a_typed_refusal(catalog):
    current, _ = catalog
    answer, api = await confirm(current, ["unknown-package"], [])
    result = json.loads(answer)
    assert api.posts == []
    assert result["status"] == "unknown_catalog_packages"
    assert result["packages"] == ["unknown-package"]


async def test_owned_seed_key_identifies_package_without_a_declaration(catalog):
    current, package = catalog
    item = next(item for item in current.packages if item.name == package)
    key = package.replace("-", "_") + "." + item.package.settings[0].name
    answer, api = await confirm(current, [], [{"key": key, "value": [], "description": "My list"}])
    result = json.loads(answer)
    assert api.posts == []
    assert [item["key"] for item in result["settings"]] == ["language"]


async def test_invalid_binding_does_not_break_ordinary_or_other_package_confirmation(catalog):
    current, package = catalog
    broken = replace(current, bindings={**current.bindings, package: "binding_version: 99"})
    for declared in ([], [next(item.name for item in current.packages if item.name != package)]):
        answer, api = await confirm(
            broken,
            declared,
            [{"key": "timezone", "value": "Europe/Nicosia", "description": "Local time"}],
        )
        assert "confirmed and frozen" in answer
        assert api.briefs[BRIEF_ID]["confirmed_at"]


async def test_declared_invalid_binding_gets_a_typed_refusal(catalog):
    current, package = catalog
    broken = replace(current, bindings={**current.bindings, package: "binding_version: 99"})
    answer, api = await confirm(broken, [package], [])
    result = json.loads(answer)
    assert api.posts == []
    assert result["status"] == "package_settings_unavailable"
    assert result["packages"] == [package]


def test_real_shape_catalog_block_budget_and_neutral_binding_descriptions(catalog):
    current, _ = catalog
    block = render_po_packages(current)
    assert len(block) < 3000
    assert "product language: one of ru, en" in block
    assert "product timezone" in block


def test_confirmation_schema_requires_explicit_package_declaration():
    schema = confirm_product_brief.tool_call_schema.model_json_schema()
    assert "catalog_packages" in schema["required"]
