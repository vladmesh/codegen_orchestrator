"""Catalog-owned product settings survive PO intake without reinterpretation."""

from dataclasses import replace
import json
from unittest.mock import AsyncMock

from langchain_core.messages import HumanMessage
import pytest
import yaml

from shared.contracts.dto.product_brief import ProductBriefContent
from src.agents.po.graph import po_prompt
from src.agents.po.tools_briefs import confirm_product_brief
from src.catalog_product_settings import PO_CATALOG_CONFIG_KEY, render_po_packages, turn_catalog
from src.kit_catalog import KitCatalogFailure, KitCatalogUnavailable
from tests.unit.po.catalog_packages import notebook_snapshot
from tests.unit.po.test_tools_briefs import (
    _API,
    BRIEF_ID,
    PROJECT_ID,
    _brief,
    _config,
    _install,
    _stored_content,
)


def notebook_content(settings):
    content = _stored_content(settings=settings)
    content["summary"] = "Store notes and list notes"
    content["must_requirements"][0].update(text="Store notes", user_wording="Store notes")
    return content


def settings():
    return [
        {
            "key": "interface_locale",
            "scope": "product",
            "value": "en",
            "description": "English replies",
        },
        {
            "key": "notebook.starting_notes",
            "scope": "product",
            "value": ["Dune"],
            "description": "Start with Dune",
        },
    ]


@pytest.fixture
def stream_client():
    return AsyncMock()


def test_turn_block_exposes_binding_and_seed_keys_with_a_compact_budget():
    catalog = notebook_snapshot()
    block = render_po_packages(catalog)
    assert "interface_locale" in block
    assert '"enum":["ru","en"]' in block
    assert "notebook.starting_notes" in block
    assert "Named notes copied once per user." in block
    assert '"required":true' in block
    assert '"seed":true' in block
    assert len(block) < 1600
    messages = po_prompt(
        {"messages": [HumanMessage(content="Hello")]},
        {"configurable": {PO_CATALOG_CONFIG_KEY: catalog}},
    )
    assert block in messages[0].content


def test_unavailable_catalog_adds_no_package_block():
    catalog = KitCatalogUnavailable("fixture", KitCatalogFailure.TRANSPORT, "offline")
    assert render_po_packages(catalog) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_scope", [False, True])
async def test_confirm_refuses_missing_package_keys(stream_client, wrong_scope):
    initial = [{"key": "notes", "value": ["Dune"], "description": "Start with Dune"}]
    if wrong_scope:
        initial = [dict(item, scope="user", subject_id=42) for item in settings()]
    api = _API(briefs={BRIEF_ID: _brief(content=notebook_content(initial))})
    _install(api, stream_client)
    config = _config()
    config["configurable"][PO_CATALOG_CONFIG_KEY] = notebook_snapshot()
    answer = json.loads(
        await confirm_product_brief.ainvoke(
            {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=config
        )
    )
    assert api.posts == []
    assert answer["status"] == "package_settings_required"
    assert {item["key"] for item in answer["settings"]} == {
        "interface_locale",
        "notebook.starting_notes",
    }
    language = next(item for item in answer["settings"] if item["key"] == "interface_locale")
    assert language["schema"]["enum"] == ["ru", "en"]
    assert "ask the user" in answer["instruction"].lower()
    assert BRIEF_ID in answer["instruction"]


@pytest.mark.asyncio
async def test_confirm_preserves_confirmed_package_values(stream_client):
    initial = settings()
    api = _API(briefs={BRIEF_ID: _brief(content=notebook_content(initial))})
    _install(api, stream_client)
    config = _config()
    config["configurable"][PO_CATALOG_CONFIG_KEY] = notebook_snapshot()
    answer = await confirm_product_brief.ainvoke(
        {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=config
    )
    assert "confirmed and frozen" in answer
    confirmed = ProductBriefContent.model_validate(api.posts[0][1]["content"])
    assert [(item.key, item.value) for item in confirmed.initial_settings] == [
        (item["key"], item["value"]) for item in initial
    ]


@pytest.mark.asyncio
async def test_confirm_refuses_a_value_outside_binding_allowed_values(stream_client):
    initial = settings()
    initial[0]["value"] = "de"
    api = _API(briefs={BRIEF_ID: _brief(content=notebook_content(initial))})
    _install(api, stream_client)
    config = _config()
    config["configurable"][PO_CATALOG_CONFIG_KEY] = notebook_snapshot()
    answer = json.loads(
        await confirm_product_brief.ainvoke(
            {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=config
        )
    )
    assert api.posts == []
    assert answer["settings"][0]["key"] == "interface_locale"
    assert answer["settings"][0]["issue"] == "invalid_value"


def test_schema_default_is_optional_and_preserved_in_the_block():
    catalog = notebook_snapshot()
    manifest = yaml.safe_load(catalog.manifests["notebook"])
    manifest["settings_schema"]["properties"]["starting_notes"]["default"] = []
    catalog = replace(catalog, manifests={"notebook": yaml.safe_dump(manifest)})
    block = render_po_packages(catalog)
    assert '"default":[]' in block
    assert '"required":false' in block


@pytest.mark.asyncio
async def test_tools_reuse_the_turn_snapshot(kit_catalog_off_github):
    catalog = notebook_snapshot()
    assert await turn_catalog({"configurable": {PO_CATALOG_CONFIG_KEY: catalog}}) is catalog
    kit_catalog_off_github.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_outage_leaves_confirmation_working(stream_client):
    api = _API(briefs={BRIEF_ID: _brief()})
    _install(api, stream_client)
    config = _config()
    config["configurable"][PO_CATALOG_CONFIG_KEY] = KitCatalogUnavailable(
        "fixture", KitCatalogFailure.TRANSPORT, "offline"
    )
    answer = await confirm_product_brief.ainvoke(
        {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=config
    )
    assert "confirmed and frozen" in answer
    assert api.posts[0][0] == f"product-briefs/{BRIEF_ID}/confirm"


@pytest.mark.asyncio
async def test_binding_key_is_read_from_this_snapshot(stream_client):
    catalog = notebook_snapshot()
    catalog = replace(
        catalog,
        bindings={
            "notebook": catalog.bindings["notebook"].replace("interface_locale", "reply_locale")
        },
    )
    api = _API(briefs={BRIEF_ID: _brief(content=notebook_content(settings()))})
    _install(api, stream_client)
    config = _config()
    config["configurable"][PO_CATALOG_CONFIG_KEY] = catalog
    answer = json.loads(
        await confirm_product_brief.ainvoke(
            {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=config
        )
    )
    assert [item["key"] for item in answer["settings"]] == ["reply_locale"]
    assert "reply_locale" in render_po_packages(catalog)
