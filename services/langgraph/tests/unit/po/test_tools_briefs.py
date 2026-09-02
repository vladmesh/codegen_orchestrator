"""The PO presents and confirms one Product Brief, and never a second reading of it.

What is proved here is the property the card exists for: between the user's
requirement and the story there is exactly one durable document, and a retry, a
restart or another PO turn re-presents *that* document instead of composing a
second interpretation of the same conversation. The endpoints these tools call
are the released ones; nothing here stores a brief of its own.
"""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shared.clients.internal_api import InternalAPIClient
from src.agents.po.tools import init_po_clients
from src.agents.po.tools_briefs import (
    PRODUCT_BRIEF_POINTER_KEY,
    confirm_product_brief,
    present_product_brief,
)

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
BRIEF_ID = "brief-1"

_REQUIREMENTS = [
    {"id": "r1", "text": "It stores a recipe", "user_wording": "I want to save my recipes"},
    {
        "id": "r2",
        "text": "It suggests a recipe every morning",
        "wording_reference": "telegram:chat=42:message=17",
    },
]


def _response(data, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json.return_value = data
    return resp


def _stored_content(summary: str = "A bot that keeps recipes", settings=None) -> dict:
    return {
        "summary": summary,
        "must_requirements": [
            {
                "id": "r1",
                "text": "It stores a recipe",
                "user_wording": "I want to save my recipes",
                "wording_reference": None,
            },
            {
                "id": "r2",
                "text": "It suggests a recipe every morning",
                "user_wording": None,
                "wording_reference": "telegram:chat=42:message=17",
            },
        ],
        "initial_settings": settings or [],
    }


def _brief(
    *,
    brief_id: str = BRIEF_ID,
    revision: int = 1,
    confirmed: bool = False,
    story_id: str | None = None,
    content: dict | None = None,
) -> dict:
    return {
        "id": brief_id,
        "project_id": PROJECT_ID,
        "story_id": story_id,
        "revision": revision,
        "title": "Recipe bot",
        "content": content or _stored_content(),
        "confirmed_at": "2026-09-02T10:00:00Z" if confirmed else None,
        "confirmation_request_id": f"po-brief-confirm:{brief_id}" if confirmed else None,
        "coverage_admitted_at": None,
        "planning_attempt_id": None,
        "planning_attempt_active": False,
        "planning_attempt_heartbeat_at": None,
    }


class _API:
    """The released routes these tools use, answered by path."""

    def __init__(self, *, project_config: dict | None = None, briefs=None, secret_keys=None):
        self.project_config = dict(project_config or {})
        self.briefs = dict(briefs or {})
        self.secret_keys = list(secret_keys or [])
        self.posts: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []
        self.post_status = HTTPStatus.CREATED
        self.post_detail: str | None = None

    async def get_raw(self, path: str, headers=None, **kwargs) -> MagicMock:
        if path == f"projects/{PROJECT_ID}":
            return _response({"id": PROJECT_ID, "config": self.project_config})
        if path == f"projects/{PROJECT_ID}/config/secrets/keys":
            return _response({"keys": self.secret_keys})
        if path.startswith("product-briefs/"):
            brief_id = path.split("/", maxsplit=1)[1]
            if brief_id not in self.briefs:
                return _response({"detail": "not found"}, status_code=HTTPStatus.NOT_FOUND)
            return _response(self.briefs[brief_id])
        raise AssertionError(f"unexpected GET {path}")

    async def post_raw(self, path: str, json: dict, headers=None, **kwargs) -> MagicMock:
        self.posts.append((path, json))
        if self.post_status >= HTTPStatus.BAD_REQUEST:
            return _response({"detail": self.post_detail}, status_code=self.post_status)
        if path == "product-briefs/":
            revision = 1 + max((b["revision"] for b in self.briefs.values()), default=0)
            brief_id = f"brief-{revision}"
            stored = _brief(brief_id=brief_id, revision=revision, content=json["content"])
            stored["title"] = json["title"]
            self.briefs[brief_id] = stored
            return _response(stored, status_code=HTTPStatus.CREATED)
        if path.endswith("/confirm"):
            brief_id = path.split("/")[1]
            self.briefs[brief_id]["confirmed_at"] = "2026-09-02T10:00:00Z"
            self.briefs[brief_id]["confirmation_request_id"] = json["request_id"]
            return _response(self.briefs[brief_id])
        raise AssertionError(f"unexpected POST {path}")

    async def patch_raw(self, path: str, json: dict, headers=None, **kwargs) -> MagicMock:
        self.patches.append((path, json))
        self.project_config = json["config"]
        return _response({"id": PROJECT_ID, "config": self.project_config})


@pytest.fixture
def stream_client() -> AsyncMock:
    client = AsyncMock()
    client.redis = AsyncMock()
    return client


def _install(api: _API, stream_client: AsyncMock) -> None:
    init_po_clients(api, stream_client)


def _config() -> dict:
    return {"configurable": {"thread_id": "po-chat-1", "telegram_chat_id": "42"}}


async def _present(**overrides) -> str:
    payload = {
        "project_id": PROJECT_ID,
        "title": "Recipe bot",
        "summary": "A bot that keeps recipes",
        "must_requirements": _REQUIREMENTS,
    }
    payload.update(overrides)
    return await present_product_brief.ainvoke(payload, config=_config())


class TestPresenting:
    @pytest.mark.asyncio
    async def test_one_message_carries_the_whole_confirmation(self, stream_client):
        """Summary, every requirement with its identity and wording, the settings."""
        api = _API()
        _install(api, stream_client)

        message = await _present(
            initial_settings=[{"key": "recipes.default_language", "value": "ru"}]
        )

        assert "A bot that keeps recipes" in message
        assert "[r1] It stores a recipe" in message
        assert 'your words: "I want to save my recipes"' in message
        assert "said in: telegram:chat=42:message=17" in message
        assert "recipes.default_language (product) = 'ru'" in message
        assert message.rstrip().endswith("yes / correct me")

    @pytest.mark.asyncio
    async def test_an_unchosen_value_is_shown_as_not_specified(self, stream_client):
        api = _API()
        _install(api, stream_client)

        message = await _present()

        assert "Initial settings:\n- not specified" in message

    @pytest.mark.asyncio
    async def test_the_revision_is_opened_through_the_released_endpoint(self, stream_client):
        api = _API()
        _install(api, stream_client)

        await _present()

        path, body = api.posts[0]
        assert path == "product-briefs/"
        assert body["project_id"] == PROJECT_ID
        assert body["request_id"] == f"po-brief:{PROJECT_ID}:r1"
        assert body["content"]["must_requirements"][0]["user_wording"] == (
            "I want to save my recipes"
        )

    @pytest.mark.asyncio
    async def test_the_project_points_at_the_presented_revision(self, stream_client):
        """Without the pointer a restarted PO could not find what it presented."""
        api = _API()
        _install(api, stream_client)

        await _present()

        assert api.project_config[PRODUCT_BRIEF_POINTER_KEY] == "brief-1"

    @pytest.mark.asyncio
    async def test_a_restart_re_presents_the_stored_revision(self, stream_client):
        """The second reading of the same conversation never reaches the user."""
        api = _API(
            project_config={PRODUCT_BRIEF_POINTER_KEY: BRIEF_ID},
            briefs={BRIEF_ID: _brief()},
        )
        _install(api, stream_client)

        message = await _present(summary="A totally different reading of the same chat")

        assert api.posts == []
        assert "A bot that keeps recipes" in message
        assert "A totally different reading" not in message

    @pytest.mark.asyncio
    async def test_a_confirmed_revision_is_not_presented_again(self, stream_client):
        api = _API(
            project_config={PRODUCT_BRIEF_POINTER_KEY: BRIEF_ID},
            briefs={BRIEF_ID: _brief(confirmed=True)},
        )
        _install(api, stream_client)

        message = await _present()

        assert api.posts == []
        assert f"create_story(product_brief_id='{BRIEF_ID}')" in message

    @pytest.mark.asyncio
    async def test_a_correction_is_a_new_revision(self, stream_client):
        """The released API has no update path, and this flow asks for none."""
        api = _API(
            project_config={PRODUCT_BRIEF_POINTER_KEY: BRIEF_ID},
            briefs={BRIEF_ID: _brief()},
        )
        _install(api, stream_client)

        message = await _present(
            summary="A bot that keeps recipes and shops for them",
            corrects_brief_id=BRIEF_ID,
        )

        path, body = api.posts[0]
        assert path == "product-briefs/"
        assert body["request_id"] == f"po-brief:{PROJECT_ID}:r2"
        assert api.briefs[BRIEF_ID]["content"]["summary"] == "A bot that keeps recipes"
        assert api.project_config[PRODUCT_BRIEF_POINTER_KEY] == "brief-2"
        assert "shops for them" in message

    @pytest.mark.asyncio
    async def test_correcting_a_superseded_revision_re_presents_the_current_one(
        self, stream_client
    ):
        api = _API(
            project_config={PRODUCT_BRIEF_POINTER_KEY: "brief-2"},
            briefs={"brief-2": _brief(brief_id="brief-2", revision=2)},
        )
        _install(api, stream_client)

        message = await _present(corrects_brief_id=BRIEF_ID)

        assert api.posts == []
        assert "superseded it" in message

    @pytest.mark.asyncio
    async def test_a_requirement_with_no_provenance_is_refused(self, stream_client):
        api = _API()
        _install(api, stream_client)

        message = await _present(must_requirements=[{"id": "r1", "text": "It stores a recipe"}])

        assert api.posts == []
        assert "No Product Brief was presented" in message

    @pytest.mark.asyncio
    async def test_an_id_that_is_not_path_safe_is_refused(self, stream_client):
        api = _API()
        _install(api, stream_client)

        message = await _present(
            must_requirements=[{"id": "r/1", "text": "It stores a recipe", "user_wording": "x"}]
        )

        assert api.posts == []
        assert "path-safe" in message

    @pytest.mark.asyncio
    async def test_a_project_secret_is_never_a_setting(self, stream_client):
        """The PO holds the token as a secret; the brief is read back by an LLM."""
        api = _API(secret_keys=["OPENROUTER_KEY", "RECIPES_FEED_ID"])
        _install(api, stream_client)

        message = await _present(initial_settings=[{"key": "recipes.feed_id", "value": "feed-7"}])

        assert api.posts == []
        assert "is a secret of this project" in message

    @pytest.mark.asyncio
    async def test_a_revision_that_already_names_other_content_is_not_replaced(self, stream_client):
        api = _API()
        api.post_status = HTTPStatus.CONFLICT
        api.post_detail = "request_id already names a different Product Brief"
        _install(api, stream_client)

        message = await _present()

        assert "No Product Brief was presented" in message
        assert api.project_config == {}


class TestConfirming:
    @pytest.mark.asyncio
    async def test_confirmation_echoes_the_stored_revision(self, stream_client):
        api = _API(
            project_config={PRODUCT_BRIEF_POINTER_KEY: BRIEF_ID},
            briefs={BRIEF_ID: _brief()},
        )
        _install(api, stream_client)

        message = await confirm_product_brief.ainvoke(
            {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=_config()
        )

        path, body = api.posts[0]
        assert path == f"product-briefs/{BRIEF_ID}/confirm"
        assert body["content"] == _stored_content()
        assert body["request_id"] == f"po-brief-confirm:{BRIEF_ID}"
        assert "confirmed and frozen" in message

    @pytest.mark.asyncio
    async def test_confirming_twice_confirms_once(self, stream_client):
        api = _API(briefs={BRIEF_ID: _brief(confirmed=True)})
        _install(api, stream_client)

        message = await confirm_product_brief.ainvoke(
            {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=_config()
        )

        assert api.posts == []
        assert "already confirmed" in message

    @pytest.mark.asyncio
    async def test_a_server_refusal_is_relayed_rather_than_worked_around(self, stream_client):
        api = _API(briefs={BRIEF_ID: _brief()})
        api.post_status = HTTPStatus.CONFLICT
        api.post_detail = "confirmation content does not match the stored revision"
        _install(api, stream_client)

        message = await confirm_product_brief.ainvoke(
            {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=_config()
        )

        assert "does not match the stored revision" in message

    @pytest.mark.asyncio
    async def test_a_brief_that_does_not_exist_confirms_nothing(self, stream_client):
        api = _API()
        _install(api, stream_client)

        message = await confirm_product_brief.ainvoke(
            {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=_config()
        )

        assert api.posts == []
        assert "Present one first" in message


@pytest.mark.asyncio
async def test_the_tools_are_wired_to_the_shared_client(stream_client):
    """A spec'd client proves the tools use only the client surface PO has."""
    api = AsyncMock(spec=InternalAPIClient)
    api.get_raw.return_value = _response({"id": PROJECT_ID, "config": {}})
    api.post_raw.return_value = _response(_brief(), status_code=HTTPStatus.CREATED)
    api.patch_raw.return_value = _response({"id": PROJECT_ID})
    init_po_clients(api, stream_client)

    message = await _present()

    assert "yes / correct me" in message
