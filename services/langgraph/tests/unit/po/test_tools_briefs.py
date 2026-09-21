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
from shared.contracts.dto.product_brief import ProposedProductBriefContent
from src.agents.po.tools_briefs import (
    LABELS,
    PRODUCT_BRIEF_POINTER_KEY,
    _creation_request_id,
    confirm_product_brief,
    present_product_brief,
)
from src.agents.po.tools_shared import init_po_clients

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

_EXAMPLES = [
    {
        "requirement_id": "r1",
        "user_sends": "the text: pancakes, flour, milk, eggs",
        "product_answers": "Saved the recipe Pancakes",
    },
    {
        "requirement_id": "r2",
        "user_sends": "nothing — at 8:00 the bot writes first",
        "product_answers": "Today's recipe: Pancakes",
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
                "user_facing": True,
            },
            {
                "id": "r2",
                "text": "It suggests a recipe every morning",
                "user_wording": None,
                "wording_reference": "telegram:chat=42:message=17",
                "user_facing": True,
            },
        ],
        "initial_settings": settings or [],
        "language": "en",
        "usage_examples": _EXAMPLES,
        "limitations": [],
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
        self.by_request_id: dict[str, dict] = {}
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
            # The released rule, modelled: `request_id` is looked up globally,
            # the same key with the same document returns the revision it
            # already opened, and the same key with a different one is refused.
            existing = self.by_request_id.get(json["request_id"])
            if existing is not None:
                same = existing["title"] == json["title"] and existing["content"] == json["content"]
                if not same:
                    return _response(
                        {"detail": "request_id already names a different Product Brief"},
                        status_code=HTTPStatus.CONFLICT,
                    )
                return _response(existing, status_code=HTTPStatus.CREATED)
            revision = 1 + max((b["revision"] for b in self.briefs.values()), default=0)
            brief_id = f"brief-{revision}"
            stored = _brief(brief_id=brief_id, revision=revision, content=json["content"])
            stored["title"] = json["title"]
            self.briefs[brief_id] = stored
            self.by_request_id[json["request_id"]] = stored
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


def _key_of(content: dict, title: str = "Recipe bot", attempt: int = 0) -> str:
    """The creation key the tool derives for this exact document."""
    return _creation_request_id(
        PROJECT_ID, title, ProposedProductBriefContent.model_validate(content), attempt
    )


def _bind_and_forget(api: _API, brief_id: str, story_id: str = "story-1") -> None:
    """What `create_story` leaves behind: a bound revision and no pointer."""
    api.briefs[brief_id]["confirmed_at"] = "2026-09-02T10:00:00Z"
    api.briefs[brief_id]["story_id"] = story_id
    api.project_config = {}
    api.posts.clear()


async def _present(**overrides) -> str:
    payload = {
        "project_id": PROJECT_ID,
        "title": "Recipe bot",
        "summary": "A bot that keeps recipes",
        "must_requirements": _REQUIREMENTS,
        "language": "en",
        "usage_examples": _EXAMPLES,
    }
    payload.update(overrides)
    return await present_product_brief.ainvoke(payload, config=_config())


def _user_part(message: str) -> str:
    """The part the PO is told to send unchanged: everything after the prefix."""
    return message.split("\n\n", maxsplit=1)[1]


class TestPresenting:
    @pytest.mark.asyncio
    async def test_one_message_carries_the_whole_confirmation(self, stream_client):
        """Summary, every requirement with its identity and wording, the settings."""
        api = _API()
        _install(api, stream_client)

        message = await _present(
            initial_settings=[
                {
                    "key": "recipes.default_language",
                    "value": "ru",
                    "description": "Recipes are written in Russian",
                }
            ],
            limitations=["Recipes are only saved from text, not from photos"],
        )

        assert "A bot that keeps recipes" in message
        assert "[r1] It stores a recipe" in message
        assert 'your words: "I want to save my recipes"' in message
        # Where the user said it is shown in words; the audit pointer is not.
        assert "[r2] It suggests a recipe every morning\n  said earlier in our conversation" in (
            message
        )
        assert "telegram:chat=42:message=17" not in message
        assert "How you will use it:\n[r1]\n  You send: the text: pancakes" in message
        assert "Limitations and chosen trade-offs:\n- Recipes are only saved from text" in message
        # The setting is shown by what it means, never by its key.
        assert "Initial settings:\n- Recipes are written in Russian" in message
        assert "recipes.default_language" not in message
        assert message.rstrip().endswith("yes / correct me")

    @pytest.mark.asyncio
    async def test_an_unchosen_value_is_shown_as_not_specified(self, stream_client):
        api = _API()
        _install(api, stream_client)

        message = await _present()

        assert "Initial settings:\n- not specified" in message

    @pytest.mark.asyncio
    async def test_a_russian_brief_is_shown_in_russian_only(self, stream_client):
        """The 2026-09-15 finance bot: English labels and `ocr.method = free`."""
        api = _API()
        _install(api, stream_client)

        message = await _present(
            title="Финансовый бот",
            summary="Бот, который считает мои доходы и расходы",
            language="ru",
            must_requirements=[
                {"id": "expense", "text": "Записывает расход", "user_wording": "считай траты"},
                {"id": "income", "text": "Записывает доход", "user_wording": "и доходы тоже"},
                {
                    "id": "backup",
                    "text": "Раз в сутки сохраняет резервную копию",
                    "user_wording": "чтобы ничего не пропало",
                    "user_facing": False,
                },
            ],
            usage_examples=[
                {
                    "requirement_id": "expense",
                    "user_sends": "фото чека",
                    "product_answers": "Записал расход 450 ₽, категория «Продукты»",
                },
                {
                    "requirement_id": "income",
                    "user_sends": "команду /income 50000 зарплата",
                    "product_answers": "Записал доход 50 000 ₽",
                },
            ],
            limitations=["Доход вводится только командой /income, не фотографией"],
            initial_settings=[
                {
                    "key": "ocr.method",
                    "value": "free",
                    "description": "Чеки распознаются бесплатным способом",
                }
            ],
        )

        shown = _user_part(message)
        assert "Как вы будете пользоваться:" in shown
        assert "[income]\n  Вы отправляете: команду /income 50000 зарплата" in shown
        assert "  Продукт отвечает: Записал доход 50 000 ₽" in shown
        assert "[backup]\n  работает без ваших сообщений" in shown
        assert "Ограничения и выбранные компромиссы:\n- Доход вводится только" in shown
        assert "- Чеки распознаются бесплатным способом" in shown
        assert "ocr.method" not in shown
        assert "(product)" not in shown
        assert " = " not in shown
        assert shown.rstrip().endswith(LABELS["ru"]["answer"])
        for english in LABELS["en"].values():
            assert english not in shown
        # What only the PO needs stays in the prefix it is not told to send.
        assert "(id: brief-1)" in message
        assert "brief-1" not in shown

    @pytest.mark.asyncio
    async def test_a_russian_brief_never_shows_a_raw_wording_reference(self, stream_client):
        """The 1293 review: «где сказано: telegram:chat=42:message=17» reached the user."""
        api = _API()
        _install(api, stream_client)

        message = await _present(
            title="Финансовый бот",
            summary="Бот, который считает мои расходы",
            language="ru",
            must_requirements=[
                {
                    "id": "expense",
                    "text": "Записывает расход",
                    "wording_reference": "telegram:chat=42:message=17",
                }
            ],
            usage_examples=[
                {
                    "requirement_id": "expense",
                    "user_sends": "текст «кофе 250»",
                    "product_answers": "Записал расход 250 ₽",
                }
            ],
        )

        shown = _user_part(message)
        assert "[expense] Записывает расход\n  сказано раньше в нашей переписке\n" in shown
        assert "telegram:chat=42:message=17" not in shown
        assert "chat=" not in shown
        # The reference itself is kept for the architect, in the stored revision.
        stored = api.briefs["brief-1"]["content"]["must_requirements"][0]
        assert stored["wording_reference"] == "telegram:chat=42:message=17"

    @pytest.mark.asyncio
    async def test_a_language_with_no_table_falls_back_to_english_labels(self, stream_client):
        api = _API()
        _install(api, stream_client)

        message = await _present(language="de")

        assert "How you will use it:" in message
        assert message.rstrip().endswith(LABELS["en"]["answer"])

    @pytest.mark.asyncio
    async def test_a_user_facing_requirement_without_an_example_is_refused(self, stream_client):
        api = _API()
        _install(api, stream_client)

        message = await _present(usage_examples=[_EXAMPLES[0]])

        assert "No Product Brief was presented" in message
        assert "no usage example: r2" in message
        assert api.posts == []
        assert api.patches == []

    @pytest.mark.asyncio
    async def test_a_brief_without_a_language_is_refused(self, stream_client):
        api = _API()
        _install(api, stream_client)

        message = await _present(language=None)

        assert "No Product Brief was presented" in message
        assert "language" in message
        assert api.posts == []

    @pytest.mark.asyncio
    async def test_a_brief_stored_before_usage_examples_still_renders(self, stream_client):
        """A revision presented before this card is re-presented, not refused."""
        legacy = {
            "summary": "A bot that keeps recipes",
            "must_requirements": [
                {"id": "r1", "text": "It stores a recipe", "user_wording": "save my recipes"}
            ],
            "initial_settings": [{"key": "recipes.default_language", "value": "ru"}],
        }
        api = _API(
            project_config={PRODUCT_BRIEF_POINTER_KEY: BRIEF_ID},
            briefs={BRIEF_ID: _brief(content=legacy)},
        )
        _install(api, stream_client)

        message = await _present()

        assert api.posts == []
        assert "[r1] It stores a recipe" in message
        assert 'recipes.default_language (product) = "ru"' in message
        assert message.rstrip().endswith("yes / correct me")

    @pytest.mark.asyncio
    async def test_the_revision_is_opened_through_the_released_endpoint(self, stream_client):
        api = _API()
        _install(api, stream_client)

        await _present()

        path, body = api.posts[0]
        assert path == "product-briefs/"
        assert body["project_id"] == PROJECT_ID
        # The key names this presentation, not a revision number the PO guessed:
        # the server owns the counter, and a guess collides with a spent key on
        # the project's second brief.
        assert body["request_id"].startswith(f"po-brief:{PROJECT_ID}:")
        assert not body["request_id"].endswith(":r1")
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
        assert body["request_id"] != _key_of(_stored_content())
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

        message = await _present(
            initial_settings=[
                {"key": "recipes.feed_id", "value": "feed-7", "description": "Recipe feed 7"}
            ]
        )

        assert api.posts == []
        assert "is a secret of this project" in message

    @pytest.mark.asyncio
    async def test_a_revision_opened_concurrently_is_relayed_not_worked_around(self, stream_client):
        api = _API()
        api.post_status = HTTPStatus.CONFLICT
        api.post_detail = "another Product Brief revision was opened concurrently; retry"
        _install(api, stream_client)

        message = await _present()

        assert "No Product Brief was presented" in message
        assert "opened concurrently" in message
        assert api.project_config == {}


class TestTheCreationKey:
    """The key names the presentation, so a project may have a second brief.

    A key derived from a guessed revision number is unique only for a project
    that never opens a second brief: the counter lives on the server, and the PO
    forgets its pointer the moment a brief is bound. The released endpoint looks
    the key up globally and refuses a spent one with 409, so the guess wedges
    every later presentation on that project — which is every feature story of a
    live product.
    """

    @pytest.mark.asyncio
    async def test_a_retry_of_the_same_presentation_opens_one_revision(self, stream_client):
        """Even with the pointer gone, the same document is the same revision."""
        api = _API()
        _install(api, stream_client)

        first = await _present()
        api.project_config = {}  # the pointer never reached the config
        second = await _present()

        assert len(api.briefs) == 1
        assert api.posts[0][1]["request_id"] == api.posts[1][1]["request_id"]
        assert first == second

    @pytest.mark.asyncio
    async def test_a_second_brief_is_presentable_once_the_first_is_bound(self, stream_client):
        """The shape of every feature story on a live project."""
        api = _API()
        _install(api, stream_client)

        await _present()
        _bind_and_forget(api, "brief-1")

        message = await _present(summary="A bot that also shops for the ingredients")

        assert set(api.briefs) == {"brief-1", "brief-2"}
        assert api.briefs["brief-2"]["revision"] == 2
        assert api.project_config[PRODUCT_BRIEF_POINTER_KEY] == "brief-2"
        assert "shops for the ingredients" in message

    @pytest.mark.asyncio
    async def test_a_spent_revision_is_reached_past_rather_than_re_presented(self, stream_client):
        """The same document asked for twice is two stories, not one."""
        api = _API()
        _install(api, stream_client)

        await _present()
        _bind_and_forget(api, "brief-1")

        message = await _present()

        assert set(api.briefs) == {"brief-1", "brief-2"}
        assert api.briefs["brief-1"]["story_id"] == "story-1"
        assert api.briefs["brief-2"]["story_id"] is None
        assert api.project_config[PRODUCT_BRIEF_POINTER_KEY] == "brief-2"
        assert "(id: brief-2)" in message

    @pytest.mark.asyncio
    async def test_a_confirmed_revision_the_pointer_lost_is_not_re_confirmed(self, stream_client):
        """The key finds what the pointer forgot, and the user is not asked twice."""
        api = _API()
        _install(api, stream_client)

        await _present()
        api.briefs["brief-1"]["confirmed_at"] = "2026-09-02T10:00:00Z"
        api.project_config = {}

        message = await _present()

        assert set(api.briefs) == {"brief-1"}
        assert "already confirmed" in message
        assert "create_story(product_brief_id='brief-1')" in message


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
    async def test_a_revision_stored_before_usage_examples_asks_for_a_correction(
        self, stream_client
    ):
        legacy = {
            "summary": "A bot that keeps recipes",
            "must_requirements": [
                {"id": "r1", "text": "It stores a recipe", "user_wording": "save my recipes"}
            ],
        }
        api = _API(briefs={BRIEF_ID: _brief(content=legacy)})
        _install(api, stream_client)

        message = await confirm_product_brief.ainvoke(
            {"project_id": PROJECT_ID, "brief_id": BRIEF_ID}, config=_config()
        )

        assert api.posts == []
        assert "was not confirmed" in message
        assert f"corrects_brief_id='{BRIEF_ID}'" in message

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
