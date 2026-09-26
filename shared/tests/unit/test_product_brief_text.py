"""The two forms of a Product Brief: one confirmation message, and the full text.

The short form is what the founder signs, so what is pinned here is what they
can and cannot see in it: bold sections in their language, each requirement's
wording once, no ids, no provenance, no filler, nothing unescaped. The full form
is pinned by its section order and where the message breaks fall. The caps are
pinned by arithmetic: a brief at every proposal cap renders a full form under
the stated ceiling, far below the 20k characters that broke the 2026-09-25
canary.
"""

from __future__ import annotations

import pytest

from shared.contracts.dto import product_brief as dto
from shared.contracts.dto.product_brief import ProductBriefContent, ProposedProductBriefContent
from shared.contracts.queues.po import MESSAGE_BREAK
from shared.product_brief_text import (
    FULL_BRIEF_CEILING,
    LABELS,
    brief_message_length,
    render_brief_message,
    render_full_brief,
    render_full_brief_sections,
)
from shared.telegram_text import utf16_length

EN = {
    "summary": "A bot that keeps recipes",
    "language": "en",
    "must_requirements": [
        {"id": "r1", "text": "It stores a recipe", "user_wording": "save my recipes"},
        {
            "id": "r2",
            "text": "It backs the recipes up every night",
            "wording_reference": "telegram:chat=42:message=17",
            "user_facing": False,
        },
    ],
    "usage_examples": [
        {
            "requirement_id": "r1",
            "user_sends": "the text: pancakes, flour, milk",
            "product_answers": "Saved the recipe Pancakes",
        }
    ],
    "limitations": ["Recipes are saved from text only, not from photos"],
    "initial_settings": [
        {"key": "recipes.language", "value": "ru", "description": "Recipes are in Russian"}
    ],
}

RU = {
    "summary": "Бот, который считает мои расходы",
    "language": "ru",
    "must_requirements": [
        {"id": "expense", "text": "Записывает расход", "user_wording": "считай траты"},
    ],
    "usage_examples": [
        {
            "requirement_id": "expense",
            "user_sends": "текст «кофе 250»",
            "product_answers": "Записал расход 250 ₽",
        }
    ],
    "limitations": ["Доход вводится только командой /income"],
    "initial_settings": [
        {"key": "ocr.method", "value": "free", "description": "Чеки распознаются бесплатно"}
    ],
}


def _content(data: dict, **overrides) -> ProposedProductBriefContent:
    return ProposedProductBriefContent.model_validate({**data, **overrides})


def _short(data: dict, title: str = "Brief", **overrides) -> str:
    return render_brief_message(title, _content(data, **overrides))


@pytest.mark.parametrize(
    ("data", "language", "sections"),
    [
        (EN, "en", ["What you get", "How you will use it", "Limitations", "Settings"]),
        (RU, "ru", ["Что вы получите", "Как вы будете пользоваться", "Ограничения", "Настройки"]),
    ],
)
def test_the_short_form_has_bold_sections_in_the_users_language(data, language, sections):
    message = _short(data)

    positions = [message.index(f"<b>{name}</b>") for name in sections]
    assert positions == sorted(positions)
    assert message.endswith(f"\n\n{LABELS[language]['answer']}")
    if language == "ru":
        for english in LABELS["en"].values():
            assert english not in message


@pytest.mark.parametrize(
    ("overrides", "absent"),
    [
        ({"limitations": []}, "<b>Limitations</b>"),
        ({"initial_settings": []}, "<b>Settings</b>"),
        ({"limitations": [], "initial_settings": []}, "<b>Settings</b>"),
    ],
)
def test_an_empty_section_is_omitted(overrides, absent):
    message = _short(EN, **overrides)

    assert absent not in message
    assert "not specified" not in message
    assert "\n\n\n" not in message


@pytest.mark.parametrize(
    ("field", "hostile"),
    [
        ("text", "It stores <script>alert(1)</script>"),
        ("text", "Fish & chips"),
        ("text", "It shows <b>bold</b> claims"),
        ("user_sends", "the text <i>x</i> & y"),
        ("summary", "A <a href='x'>bot</a> & more"),
    ],
)
def test_user_and_model_text_is_escaped(field, hostile):
    data = {**EN, "must_requirements": [dict(r) for r in EN["must_requirements"]]}
    data["usage_examples"] = [dict(e) for e in EN["usage_examples"]]
    if field == "text":
        data["must_requirements"][0]["text"] = hostile
    elif field == "user_sends":
        data["usage_examples"][0]["user_sends"] = hostile
    else:
        data[field] = hostile

    for text in (_short(data, title="<b>Title</b>"), render_full_brief("T", _content(data))):
        assert hostile not in text
        for raw in ("<script>", "<i>", "<a ", "& "):
            assert raw not in text
    message = _short(data, title="<b>Title</b>")
    assert "&lt;b&gt;Title&lt;/b&gt;" in message
    assert "&amp;" in message or "&lt;" in message


@pytest.mark.parametrize("data", [EN, RU])
def test_no_requirement_text_appears_twice(data):
    message = _short(data)

    for requirement in data["must_requirements"]:
        assert message.count(requirement["text"]) == 1
    for example in data["usage_examples"]:
        assert message.count(example["user_sends"]) == 1


@pytest.mark.parametrize("data", [EN, RU])
def test_no_id_provenance_or_filler_reaches_the_user(data):
    message = _short(data)

    for requirement in data["must_requirements"]:
        assert f"[{requirement['id']}]" not in message
        if requirement.get("user_wording"):
            assert requirement["user_wording"] not in message
    for filler in ("not specified", "не указано", "your words", "ваши слова", "said earlier"):
        assert filler not in message
    assert "telegram:chat" not in message
    assert "revision" not in message.lower()


def test_a_requirement_the_user_never_touches_says_so_on_its_own_line():
    message = _short(EN)

    assert "• It backs the recipes up every night — works without anything sent by you" in (message)


def test_a_brief_stored_before_language_renders_with_what_it_has():
    legacy = ProductBriefContent.model_validate(
        {
            "summary": "A bot that keeps recipes",
            "must_requirements": [{"id": "r1", "text": "It stores a recipe"}],
            "initial_settings": [{"key": "recipes.language", "value": None}],
        }
    )

    message = render_brief_message("Recipe bot", legacy)

    assert "<b>What you get</b>\n• It stores a recipe" in message
    assert "• recipes.language = —" in message
    assert "<b>How you will use it</b>" not in message
    assert message.endswith("yes / correct me")


def test_the_length_is_counted_as_telegram_counts_it():
    message = _short(RU, summary="Бот 🙂")

    assert brief_message_length(message) == utf16_length(message) == len(message) + 1


class TestTheFullForm:
    def test_sections_are_in_reading_order_and_each_is_one_message(self):
        sections = render_full_brief_sections("Recipe bot", _content(EN))

        assert [section.split("\n", maxsplit=1)[0] for section in sections] == [
            "<b>Recipe bot</b>",
            "<b>What you get</b>",
            "<b>How you will use it</b>",
            "<b>Limitations</b>",
            "<b>Settings</b>",
        ]
        text = render_full_brief("Recipe bot", _content(EN))
        assert text == MESSAGE_BREAK.join(sections)
        assert text.count(MESSAGE_BREAK) == len(sections) - 1
        assert not text.startswith(MESSAGE_BREAK)
        assert not text.endswith(MESSAGE_BREAK)
        assert all(MESSAGE_BREAK not in section for section in sections)

    def test_an_empty_section_is_omitted_and_no_break_is_doubled(self):
        text = render_full_brief("Recipe bot", _content(EN, limitations=[]))

        assert "<b>Limitations</b>" not in text
        assert MESSAGE_BREAK * 2 not in text
        assert text.count(MESSAGE_BREAK) == 3

    def test_the_full_form_carries_the_users_own_words(self):
        sections = render_full_brief_sections("Recipe bot", _content(EN))

        requirements = sections[1]
        assert "• It stores a recipe\n  your words: «save my recipes»" in requirements
        assert "• It backs the recipes up every night" in requirements
        assert "said earlier in our conversation" in requirements
        # The audit pointer is for the architect, not for the user.
        assert "telegram:chat" not in requirements

    def test_a_section_over_the_bots_limit_is_left_whole(self):
        many = [
            {
                "id": f"r{index}",
                "text": "x" * dto.MAX_REQUIREMENT_TEXT_LENGTH,
                "user_wording": "w" * dto.MAX_USER_WORDING_LENGTH,
                "user_facing": False,
            }
            for index in range(dto.MAX_MUST_REQUIREMENTS)
        ]
        sections = render_full_brief_sections(
            "T", _content(EN, must_requirements=many, usage_examples=[])
        )

        assert utf16_length(sections[1]) > 4000
        assert sections[1].count("x" * dto.MAX_REQUIREMENT_TEXT_LENGTH) == len(many)


def _at_every_cap() -> tuple[str, ProposedProductBriefContent]:
    requirements = dto.MAX_MUST_REQUIREMENTS
    content = ProposedProductBriefContent.model_validate(
        {
            "summary": "s" * dto.MAX_SUMMARY_LENGTH,
            "language": "ru",
            "must_requirements": [
                {
                    "id": f"r{index}",
                    "text": "t" * dto.MAX_REQUIREMENT_TEXT_LENGTH,
                    "user_wording": "w" * dto.MAX_USER_WORDING_LENGTH,
                }
                for index in range(requirements)
            ],
            "usage_examples": [
                {
                    "requirement_id": f"r{index % requirements}",
                    "user_sends": "u" * dto.MAX_USER_SENDS_LENGTH,
                    "product_answers": "a" * dto.MAX_PRODUCT_ANSWERS_LENGTH,
                }
                for index in range(dto.MAX_USAGE_EXAMPLES)
            ],
            "limitations": ["l" * dto.MAX_LIMITATION_LENGTH] * dto.MAX_LIMITATIONS,
            "initial_settings": [
                {
                    "key": f"product.setting_{index}",
                    "value": index,
                    "description": "d" * dto.MAX_SETTING_DESCRIPTION_LENGTH,
                }
                for index in range(dto.MAX_INITIAL_SETTINGS)
            ],
        }
    )
    return "T" * dto.MAX_BRIEF_TITLE_LENGTH, content


def test_the_worst_case_full_form_stays_under_the_stated_ceiling():
    """The arithmetic the caps exist for, pinned.

    Content at every cap is 100 + 400 + 8 x (200 + 250) + 10 x (150 + 200)
    + 5 x 200 + 6 x 150 = 9500 characters; labels, bullets and markup add the
    rest. The ceiling is well under the 20k brief that broke the canary.
    """
    title, content = _at_every_cap()
    raw = (
        dto.MAX_BRIEF_TITLE_LENGTH
        + dto.MAX_SUMMARY_LENGTH
        + dto.MAX_MUST_REQUIREMENTS
        * (dto.MAX_REQUIREMENT_TEXT_LENGTH + dto.MAX_USER_WORDING_LENGTH)
        + dto.MAX_USAGE_EXAMPLES * (dto.MAX_USER_SENDS_LENGTH + dto.MAX_PRODUCT_ANSWERS_LENGTH)
        + dto.MAX_LIMITATIONS * dto.MAX_LIMITATION_LENGTH
        + dto.MAX_INITIAL_SETTINGS * dto.MAX_SETTING_DESCRIPTION_LENGTH
    )
    assert raw == 9500

    full = render_full_brief(title, content)

    assert raw < utf16_length(full) <= FULL_BRIEF_CEILING
    assert FULL_BRIEF_CEILING <= 12_000
