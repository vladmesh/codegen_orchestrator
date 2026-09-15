"""The 2026-09-15 finance-bot request, shared by its scripted and real-LLM replays.

A tester asked, in Russian, for a personal finance bot: expenses and incomes by
text, expenses from receipt photos and bank screenshots, and chose free
recognition twice. The brief the PO presented said only "recognize expenses from
text", nobody decided whether income can be free text, and nobody told the user
free recognition is noticeably worse on receipts. The two checks below are the
two facts a compliant brief carries; both replays assert them on the brief the
tool actually stored.
"""

from __future__ import annotations

import re

from shared.contracts.dto.product_brief import ProductBriefContent

TELEGRAM_CHAT_ID = "42"

USER_MESSAGES = [
    "Хочу бота для учёта личных финансов. Расходы и доходы записывать обычным текстом, "
    "а расходы ещё по фото чеков и скриншотам из банка.",
    "Распознавание фото — давай бесплатный вариант.",
    "Да, бесплатный. Остальное реши сам и покажи, что получилось.",
]

#: What the user says when the PO has still not presented a brief.
NUDGE = "Покажи итоговое описание бота на подтверждение."

_INCOME = re.compile(r"доход|зарплат|преми|income|salary", re.IGNORECASE)
_COMMAND = re.compile(r"(^|[\s«\"'])/[a-z_]+", re.IGNORECASE)
_FREE_TEXT = re.compile(
    r"текст|сообщени|свободн\w*\s+форм|произвольн\w*\s+форм|text|message|free[\s-]form",
    re.IGNORECASE,
)
#: An explicit refusal; "только"/"only" alone is too vague to decide anything.
_NOT_SUPPORTED = re.compile(
    r"не\s+поддерж|нельзя|невозможно|не\s+(?:можете|получится|принима|распозна|записыва)"
    r"|not\s+supported|cannot|can't|is\s+not\s+(?:accepted|recognized)",
    re.IGNORECASE,
)
_FREE = re.compile(r"бесплатн|free", re.IGNORECASE)
_RECOGNITION = re.compile(r"распозна|чек|фото|скриншот|ocr|receipt|photo", re.IGNORECASE)


def user_message(index: int, text: str, project_id: str) -> str:
    """One user turn in the shape the PO consumer delivers it."""
    context = f"[context: telegram_chat_id={TELEGRAM_CHAT_ID}, user_name=Тестер]"
    if index == 0:
        # The token and project steps are not what this replay is about.
        context += f" (проект уже создан: project_id={project_id}, токен бота уже проверен)"
    return f"[2026-09-15T10:{index:02d}:00+00:00 UTC] {context} {text}"


def income_by_free_text_is_decided(content: ProductBriefContent) -> bool:
    """A usage example adds income as free text, or a limitation refuses free-text income."""
    for example in content.usage_examples:
        exchange = f"{example.user_sends} {example.product_answers}"
        if _INCOME.search(exchange) and not _COMMAND.search(example.user_sends):
            return True
    return any(
        _INCOME.search(limitation)
        and _FREE_TEXT.search(limitation)
        and _NOT_SUPPORTED.search(limitation)
        for limitation in content.limitations
    )


def ocr_trade_off_is_named(content: ProductBriefContent) -> bool:
    """A limitation says the free recognition of photos is the chosen trade-off."""
    return any(
        _FREE.search(limitation) and _RECOGNITION.search(limitation)
        for limitation in content.limitations
    )
