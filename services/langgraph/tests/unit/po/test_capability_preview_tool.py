"""What the PO model sees of capabilities: ids, routes, questions, answers — never packages.

Everything is exercised through the model-visible surface: the system prompt the PO
graph builds, the tools' argument schemas, and the text each tool returns. The API is a
fake answering the released routes by path; the preview itself is the real Architect
resolver over the activated catalog's genuine bytes.
"""

from __future__ import annotations

from http import HTTPStatus
import json
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import SystemMessage
import pytest

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.capability_preview import CapabilityPreviewCreate
from src.agents.po.graph import po_prompt
from src.agents.po.tools_briefs import confirm_product_brief, present_product_brief
from src.agents.po.tools_capabilities import MODULE_ROLLOUT_CONFIG_KEY, preview_capabilities
from src.agents.po.tools_shared import init_po_clients
from src.capability_preview import capability_id
from src.catalog_product_settings import PO_CATALOG_CONFIG_KEY
from src.kit_catalog import KitCatalogFailure, KitCatalogUnavailable

PROJECT_ID = "5b1d0c9e-0f6a-4c55-9a43-6f0f1a7e2b10"
CHANNELS = capability_id("tg-channels")
#: What must never reach the PO model: package, distribution, version, key, recipe, commit.
TECHNICAL = (
    "tg-channels",
    "tg_channels",
    "codegen-kit",
    "0.1.2",
    "packages/",
    "starting_channels",
    "kit add",
    CATALOG_ACTIVATION.commit[:7],
)


def _response(data, status_code: int = HTTPStatus.OK) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = data
    return response


class _API:
    def __init__(self, *, rollout=None, modules=("backend", "tg_bot")) -> None:
        self.rollout = {"project_ids": [PROJECT_ID]} if rollout is None else rollout
        self.modules = list(modules)
        self.posts: list[tuple[str, dict, dict | None]] = []
        self.answer: tuple[int, dict] | None = None

    async def get_raw(self, path: str, headers=None, **kwargs) -> MagicMock:
        if path == f"projects/{PROJECT_ID}":
            return _response({"id": PROJECT_ID, "config": {"modules": self.modules}})
        if path == f"system-configs/{MODULE_ROLLOUT_CONFIG_KEY}":
            assert headers is None, "the rollout is platform configuration, read as the service"
            if self.rollout == "unreadable":
                return _response({"detail": "boom"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return _response({"key": MODULE_ROLLOUT_CONFIG_KEY, "value": self.rollout})
        raise AssertionError(f"unexpected GET {path}")

    async def post_raw(self, path: str, json: dict, headers=None, **kwargs) -> MagicMock:
        self.posts.append((path, json, headers))
        if self.answer is not None:
            return _response(*reversed(self.answer))
        if path == "capability-previews/":
            stored = CapabilityPreviewCreate.model_validate(json)
            return _response(
                {
                    "preview_id": "preview-" + "a" * 24,
                    "project_id": PROJECT_ID,
                    "created_at": "2026-10-10T00:00:00Z",
                    **stored.product.model_dump(mode="json"),
                },
                HTTPStatus.CREATED,
            )
        raise AssertionError(f"unexpected POST {path}")


@pytest.fixture
def api() -> _API:
    api = _API()
    init_po_clients(api, AsyncMock())
    return api


def _config(catalog) -> dict:
    return {
        "configurable": {
            "thread_id": "po-chat-1",
            "telegram_chat_id": "42",
            PO_CATALOG_CONFIG_KEY: catalog,
        }
    }


async def _preview(catalog, *requests) -> str:
    return await preview_capabilities.ainvoke(
        {"project_id": PROJECT_ID, "requests": list(requests)}, config=_config(catalog)
    )


CHANNEL_REQUEST = {
    "request_id": "channels",
    "capability_id": CHANNELS,
    "wording": "дайджест каналов Кипра",
}


def test_the_po_prompt_lists_capabilities_without_packages(activated_kit_catalog):
    [system] = po_prompt(
        {"messages": []}, {"configurable": {PO_CATALOG_CONFIG_KEY: activated_kit_catalog}}
    )
    assert isinstance(system, SystemMessage)
    assert f"- {CHANNELS}: Public Telegram channel lists" in system.content
    assert "preview_capabilities" in system.content
    for technical in TECHNICAL:
        assert technical not in system.content


def test_without_the_catalog_the_prompt_promises_no_ready_module():
    unavailable = KitCatalogUnavailable("https://kit.invalid", KitCatalogFailure.TRANSPORT, "x")
    [system] = po_prompt({"messages": []}, {"configurable": {PO_CATALOG_CONFIG_KEY: unavailable}})
    assert "cannot be read right now. Do not promise a ready module" in system.content


def test_the_tool_schemas_carry_no_package_argument():
    assert set(preview_capabilities.args) == {"project_id", "requests"}
    assert set(confirm_product_brief.args) == {"project_id", "brief_id"}
    assert "capabilities" in present_product_brief.args
    for tool in (preview_capabilities, present_product_brief, confirm_product_brief):
        schema = json.dumps(tool.tool_call_schema.model_json_schema())
        assert "catalog_packages" not in schema and "tg-channels" not in schema


@pytest.mark.asyncio
async def test_a_preview_answers_routes_questions_and_limits_in_product_terms(
    api, activated_kit_catalog
):
    answer = await _preview(activated_kit_catalog, CHANNEL_REQUEST)

    result = json.loads(answer)
    assert result["status"] == "previewed" and result["preview_id"].startswith("preview-")
    assert [(r["request_id"], r["route"]) for r in result["routes"]] == [("channels", "module")]
    assert [q["question_id"] for q in result["questions"]] == ["product_language", "channels.q1"]
    assert result["questions"][0]["choices"] == ["ru", "en"]
    assert {item["name"] for item in result["limitations"]} >= {"channels_max"}
    for technical in TECHNICAL:
        assert technical not in answer


@pytest.mark.asyncio
async def test_the_technical_half_is_stored_by_the_platform_itself(api, activated_kit_catalog):
    await _preview(activated_kit_catalog, CHANNEL_REQUEST)

    [(path, body, headers)] = api.posts
    assert path == "capability-previews/" and headers is None
    [module] = body["technical"]["modules"]
    assert module["install"]["package"]["tag"] == "packages/tg-channels/v0.1.2"
    assert module["install"]["catalog"]["commit"] == body["technical"]["activation"]["commit"]


@pytest.mark.asyncio
async def test_outside_the_rollout_a_channel_capability_is_impossible(activated_kit_catalog):
    api = _API(rollout={"project_ids": []})
    init_po_clients(api, AsyncMock())
    result = json.loads(await _preview(activated_kit_catalog, CHANNEL_REQUEST))
    assert [(r["route"], r["reason"]) for r in result["routes"]] == [
        ("impossible", "rollout_not_enabled")
    ]
    assert result["questions"] == []


@pytest.mark.asyncio
async def test_an_unreadable_rollout_stores_no_preview(activated_kit_catalog):
    api = _API(rollout="unreadable")
    init_po_clients(api, AsyncMock())
    result = json.loads(await _preview(activated_kit_catalog, CHANNEL_REQUEST))
    assert (result["status"], result["code"]) == ("preview_refused", "rollout_unavailable")
    assert api.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code",
    [
        (KitCatalogFailure.TRANSPORT, "catalog_unavailable"),
        (KitCatalogFailure.PROVENANCE, "catalog_inactive"),
    ],
)
async def test_without_the_activated_catalog_nothing_is_previewed(api, failure, code):
    unavailable = KitCatalogUnavailable("https://kit.invalid", failure, "ConnectError: secret")
    answer = await _preview(unavailable, CHANNEL_REQUEST)
    result = json.loads(answer)
    assert (result["status"], result["code"], result["request_ids"]) == (
        "preview_refused",
        code,
        ["channels"],
    )
    assert "kit.invalid" not in answer and "ConnectError" not in answer
    assert api.posts == []


@pytest.mark.asyncio
async def test_an_invented_capability_id_is_refused(api, activated_kit_catalog):
    answer = await _preview(activated_kit_catalog, CHANNEL_REQUEST | {"capability_id": "cap-0"})
    assert "No preview was stored" in answer and api.posts == []
    answer = await _preview(
        activated_kit_catalog, CHANNEL_REQUEST | {"capability_id": "cap-000000000000"}
    )
    assert json.loads(answer)["code"] == "unknown_capability" and api.posts == []


_CAPABILITIES = {
    "preview_id": "preview-" + "a" * 24,
    "capabilities": [
        {
            "request_id": "channels",
            "capability_id": CHANNELS,
            "route": "module",
            "requirement_ids": ["r1"],
        }
    ],
    "answers": [
        {
            "question_id": "product_language",
            "kind": "product_language",
            "value": "en",
            "description": "The bot talks to everyone in English",
        }
    ],
}
_BRIEF = {
    "project_id": PROJECT_ID,
    "title": "Cyprus channels",
    "summary": "Digests of public channels",
    "must_requirements": [
        {"id": "r1", "text": "Shows a channel digest", "user_wording": "дайджест каналов"}
    ],
    "language": "en",
    "usage_examples": [
        {"requirement_id": "r1", "user_sends": "/digest", "product_answers": "The latest posts"}
    ],
}


class _BriefAPI(_API):
    """The brief routes, answering the capability refusal the API would give."""

    def __init__(self, refusal: dict | None = None, status: int = HTTPStatus.CREATED) -> None:
        super().__init__()
        self.refusal, self.status = refusal, status
        self.briefs: dict[str, dict] = {}

    async def get_raw(self, path: str, headers=None, **kwargs) -> MagicMock:
        if path == f"projects/{PROJECT_ID}":
            return _response({"id": PROJECT_ID, "config": {}})
        if path.startswith("product-briefs/"):
            return _response(self.briefs[path.split("/")[1]])
        return await super().get_raw(path, headers, **kwargs)

    async def post_raw(self, path: str, json: dict, headers=None, **kwargs) -> MagicMock:
        self.posts.append((path, json, headers))
        if self.refusal is not None:
            return _response({"detail": {"capability_refusal": self.refusal}}, self.status)
        brief = {
            "id": "brief-1",
            "project_id": PROJECT_ID,
            "story_id": None,
            "revision": 1,
            "title": json["title"],
            "content": json["content"],
            "planning_attempt_active": False,
        }
        self.briefs["brief-1"] = brief
        return _response(brief, HTTPStatus.CREATED)

    async def patch_raw(self, path: str, json: dict, headers=None, **kwargs) -> MagicMock:
        return _response({"id": PROJECT_ID, "config": {}})


@pytest.mark.asyncio
async def test_a_capability_brief_shows_the_answer_and_the_chosen_language():
    api = _BriefAPI()
    init_po_clients(api, AsyncMock())
    answer = await present_product_brief.ainvoke(
        _BRIEF | {"capabilities": _CAPABILITIES}, config=_config(None)
    )

    [(path, body, _)] = api.posts
    assert path == "product-briefs/"
    assert body["content"]["capabilities"] == _CAPABILITIES
    assert "The bot talks to everyone in English (English)" in answer
    for technical in TECHNICAL:
        assert technical not in answer


@pytest.mark.asyncio
async def test_a_refused_capability_brief_is_explained_without_technical_detail():
    refusal = {"code": "missing_answer", "request_ids": [], "question_ids": ["product_language"]}
    api = _BriefAPI(refusal, HTTPStatus.UNPROCESSABLE_ENTITY)
    init_po_clients(api, AsyncMock())
    answer = await present_product_brief.ainvoke(
        _BRIEF | {"capabilities": _CAPABILITIES}, config=_config(None)
    )

    assert answer.startswith("No Product Brief was presented and nothing was changed: ")
    result = json.loads(answer.split(": ", 1)[1])
    assert (result["code"], result["question_ids"]) == ("missing_answer", ["product_language"])
    assert "Ask the user each listed required question explicitly" in result["instruction"]


@pytest.mark.asyncio
async def test_confirmation_relays_a_drifted_plan_as_a_new_revision_to_present():
    api = _BriefAPI()
    init_po_clients(api, AsyncMock())
    await present_product_brief.ainvoke(
        _BRIEF | {"capabilities": _CAPABILITIES}, config=_config(None)
    )
    api.refusal = {"code": "plan_drift", "request_ids": [], "question_ids": []}
    api.status = HTTPStatus.CONFLICT

    answer = await confirm_product_brief.ainvoke(
        {"project_id": PROJECT_ID, "brief_id": "brief-1"}, config=_config(None)
    )

    assert "was not confirmed" in answer and '"code": "plan_drift"' in answer
    assert "corrects_brief_id='brief-1'" in answer
