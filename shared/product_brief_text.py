"""The two texts a Product Brief is shown as: the short form and the full form.

**The short form** is the one confirmation message the founder signs. It fits a
single Telegram message by construction: `render_brief_message` renders it and
`brief_message_length` measures it against `BRIEF_MESSAGE_BUDGET`, and a brief
whose short form does not fit is never opened as a revision — the PO stages the
product instead of squeezing the wording (`present_product_brief`). Each
requirement's wording appears once; no id, no provenance, no filler for an
empty section.

**The full form** is every section at full length, for the user who asks for it
and for the admin API. `render_full_brief_sections` returns it as a list, one
section per item; joined with ``MESSAGE_BREAK`` each section starts a new
Telegram message, and a section still over the bot's limit is cut by the bot's
own splitter (`shared.telegram_text`).

Both are pure functions of the stored title and content, and both are Telegram
HTML: every text from the user or the model is escaped with `html.escape`.
Quotes are left as they are (`quote=False`) — they are only special inside an
attribute, and the brief puts no text into one.
"""

from __future__ import annotations

import html
import json

from shared.contracts.dto.product_brief import ProductBriefContent
from shared.contracts.queues.po import MESSAGE_BREAK
from shared.telegram_text import utf16_length

#: The most a short form may measure (`brief_message_length`). Well under
#: Telegram's 4096, so the confirmation is always exactly one message.
BRIEF_MESSAGE_BUDGET = 3500

#: The stated ceiling of a full form rendered from a brief at every proposal cap
#: (`shared.contracts.dto.product_brief`), counted as Telegram counts the text
#: the user reads. Pinned by `test_product_brief_text`.
FULL_BRIEF_CEILING = 12_000

#: Every fixed label of both forms, per language. A language with no table here
#: falls back to `en`.
LABELS: dict[str, dict[str, str]] = {
    "en": {
        "what_you_get": "What you get",
        "usage": "How you will use it",
        "limitations": "Limitations",
        "settings": "Settings",
        "you_send": "You send",
        "product_answers": "The product answers",
        "no_interaction": "works without anything sent by you",
        "your_words": "your words",
        "said_in": "said earlier in our conversation",
        "answer": "yes / correct me",
        "full_brief_unavailable": (
            "Sorry, I could not open the full brief right now. Please ask me again in a minute."
        ),
    },
    "ru": {
        "what_you_get": "Что вы получите",
        "usage": "Как вы будете пользоваться",
        "limitations": "Ограничения",
        "settings": "Настройки",
        "you_send": "Вы отправляете",
        "product_answers": "Продукт отвечает",
        "no_interaction": "работает без ваших сообщений",
        "your_words": "ваши слова",
        "said_in": "сказано раньше в нашей переписке",
        "answer": "да / поправить",
        "full_brief_unavailable": (
            "Извините, сейчас не получилось открыть полный бриф. Попросите меня ещё раз "
            "через минуту."
        ),
    },
}


def labels_for(language: str | None) -> dict[str, str]:
    """The label table of the brief's language, or `en` when there is none."""
    if language is None:
        return LABELS["en"]
    return LABELS.get(language) or LABELS.get(language.split("-", maxsplit=1)[0]) or LABELS["en"]


def _e(text: str) -> str:
    return html.escape(text, quote=False)


def _heading(title: str, content: ProductBriefContent) -> str:
    return f"<b>{_e(title)}</b>\n{_e(content.summary)}"


def _section(name: str, lines: list[str]) -> str | None:
    """A bold section, or None when it has nothing to say."""
    if not lines:
        return None
    return "\n".join([f"<b>{_e(name)}</b>", *lines])


def _requirement_lines(
    content: ProductBriefContent, label: dict[str, str], full: bool
) -> list[str]:
    exemplified = {example.requirement_id for example in content.usage_examples}
    lines = []
    for requirement in content.must_requirements:
        line = f"• {_e(requirement.text)}"
        if not requirement.user_facing and requirement.id not in exemplified:
            line += f" — {_e(label['no_interaction'])}"
        lines.append(line)
        if not full:
            continue
        if requirement.user_wording:
            lines.append(f"  {_e(label['your_words'])}: «{_e(requirement.user_wording)}»")
        elif requirement.wording_reference:
            # The reference is an audit pointer (`telegram:chat=42:message=17`)
            # for the architect, not something the user can read.
            lines.append(f"  {_e(label['said_in'])}")
    return lines


def _usage_lines(content: ProductBriefContent, label: dict[str, str]) -> list[str]:
    """Every usage example once, in the order of the requirements it shows."""
    order = {requirement.id: index for index, requirement in enumerate(content.must_requirements)}
    examples = sorted(
        content.usage_examples, key=lambda example: order.get(example.requirement_id, len(order))
    )
    lines = []
    for example in examples:
        lines.append(f"• {_e(label['you_send'])}: {_e(example.user_sends)}")
        lines.append(f"  {_e(label['product_answers'])}: {_e(example.product_answers)}")
    return lines


def _setting_lines(content: ProductBriefContent) -> list[str]:
    lines = []
    for setting in content.initial_settings:
        if setting.description is not None:
            lines.append(f"• {_e(setting.description)}")
            continue
        # A document stored before settings carried a description: its key is
        # the only name it has.
        value = "—" if setting.value is None else json.dumps(setting.value, ensure_ascii=False)
        lines.append(f"• {_e(setting.key)} = {_e(value)}")
    return lines


def _sections(title: str, content: ProductBriefContent, *, full: bool) -> list[str]:
    label = labels_for(content.language)
    sections = [
        _heading(title, content),
        _section(label["what_you_get"], _requirement_lines(content, label, full)),
        _section(label["usage"], _usage_lines(content, label)),
        _section(label["limitations"], [f"• {_e(item)}" for item in content.limitations]),
        _section(label["settings"], _setting_lines(content)),
    ]
    return [section for section in sections if section is not None]


def render_brief_message(title: str, content: ProductBriefContent) -> str:
    """The short form: the one confirmation message the user is shown.

    Bold sections in the user's language — what they get, how they will use
    it, the limitations, the settings — each omitted when empty, and the answer
    line in their language at the end. Each requirement's wording appears once
    and its usage appears once, in the usage section.
    """
    label = labels_for(content.language)
    return "\n\n".join([*_sections(title, content, full=False), _e(label["answer"])])


def brief_message_length(message: str) -> int:
    """How long a short form is, as Telegram counts a message (UTF-16 units)."""
    return utf16_length(message)


def render_full_brief_sections(title: str, content: ProductBriefContent) -> list[str]:
    """The full form, one section per item, in the order the user reads them.

    The heading (title and summary), what they get with the user's own words for
    each requirement, how they will use it, the limitations and the settings.
    An empty section is omitted. Nothing is cut: a section over Telegram's limit
    is left to the bot's splitter.
    """
    return _sections(title, content, full=True)


def render_full_brief(title: str, content: ProductBriefContent) -> str:
    """The full form as one text, each section starting a new Telegram message."""
    return MESSAGE_BREAK.join(render_full_brief_sections(title, content))


def full_brief_unavailable(language: str | None) -> str:
    """The fixed reply when the full form cannot be read: no error text, no id.

    It is what the user reads in place of the full form, so it says only that
    the brief could not be opened, in the brief's language or `en`.
    """
    return labels_for(language)["full_brief_unavailable"]


__all__ = [
    "BRIEF_MESSAGE_BUDGET",
    "FULL_BRIEF_CEILING",
    "LABELS",
    "brief_message_length",
    "full_brief_unavailable",
    "labels_for",
    "render_brief_message",
    "render_full_brief",
    "render_full_brief_sections",
]
