"""A scripted replay of the 2026-09-15 finance-bot dialogue through the real PO graph.

The model is scripted to make the `present_product_brief` call a PO following
the prompt makes; everything past the model is real — the graph the consumer
builds, its tool node, the tool, its validation and its render. What is asserted
is the brief the tool stored and the message the user is shown, with the same
checks the opt-in real-LLM replay (`tests/e2e/test_po_finance_bot_brief.py`)
applies to a live model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from shared.contracts.dto.product_brief import ProductBriefContent
from src.agents.po.graph import create_po_graph
from src.agents.po.tools_shared import init_po_clients
from tests.unit.po.finance_bot_replay import (
    TELEGRAM_CHAT_ID,
    USER_MESSAGES,
    income_by_free_text_is_decided,
    ocr_trade_off_is_named,
    user_message,
)
from tests.unit.po.test_tools_briefs import _API, PROJECT_ID
from tests.unit.test_architect_graph import _ScriptedToolCallingModel

_WORDING = "Расходы и доходы записывать обычным текстом"

_COMPLIANT_BRIEF_CALL = {
    "name": "present_product_brief",
    "args": {
        "project_id": PROJECT_ID,
        "title": "Бот личных финансов",
        "summary": "Бот записывает мои расходы и доходы из текста и расходы из фото чеков.",
        "language": "ru",
        "must_requirements": [
            {"id": "expense-text", "text": "Записывает расход из текста", "user_wording": _WORDING},
            {"id": "income-text", "text": "Записывает доход из текста", "user_wording": _WORDING},
            {
                "id": "expense-photo",
                "text": "Записывает расход по фото чека или скриншоту из банка",
                "wording_reference": f"telegram:chat={TELEGRAM_CHAT_ID}:message=1",
            },
        ],
        "usage_examples": [
            {
                "requirement_id": "expense-text",
                "user_sends": "текст «кофе 250»",
                "product_answers": "Записал расход 250 ₽",
            },
            {
                "requirement_id": "income-text",
                "user_sends": "текст «зарплата 80000»",
                "product_answers": "Записал доход 80 000 ₽",
            },
            {
                "requirement_id": "expense-photo",
                "user_sends": "фото чека",
                "product_answers": "Записал расход 1 240 ₽ по чеку",
            },
        ],
        "limitations": [
            "Чеки распознаются бесплатным способом, поэтому мятый или размытый чек может "
            "прочитаться с ошибками; позже можно подключить платное распознавание."
        ],
        "initial_settings": [
            {
                "key": "ocr.method",
                "value": "free",
                "description": "Фото чеков распознаются бесплатным способом",
            }
        ],
    },
    "id": "call-brief-1",
}

_SCRIPT = [
    AIMessage(content="Доходы тоже можно писать просто текстом, например «зарплата 80000»?"),
    AIMessage(
        content="Бесплатное распознавание заметно хуже читает мятые чеки; платное можно "
        "подключить позже. Оставляем бесплатное?"
    ),
    AIMessage(content="", tool_calls=[_COMPLIANT_BRIEF_CALL]),
    AIMessage(content="Вот описание бота — подтвердите или поправьте."),
]


@pytest.mark.asyncio
async def test_the_replayed_dialogue_presents_a_brief_with_income_form_and_ocr_trade_off():
    api = _API()
    init_po_clients(api, AsyncMock())
    model = _ScriptedToolCallingModel(turns=list(_SCRIPT))
    config = {"configurable": {"thread_id": "po-finance-bot", "telegram_chat_id": TELEGRAM_CHAT_ID}}

    with patch("src.agents.po.graph.ChatOpenAI", return_value=model):
        graph = await create_po_graph(model="scripted", base_url="http://llm.invalid", api_key="x")
    for index, text in enumerate(USER_MESSAGES):
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content=user_message(index, text, PROJECT_ID))]},
            config=config,
        )

    (stored,) = api.briefs.values()
    content = ProductBriefContent.model_validate(stored["content"])
    assert income_by_free_text_is_decided(content)
    assert ocr_trade_off_is_named(content)

    (presented,) = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    shown = presented.content.split("\n\n", maxsplit=1)[1]
    assert "[income-text]\n  Вы отправляете: текст «зарплата 80000»" in shown
    assert "Ограничения и выбранные компромиссы:\n- Чеки распознаются бесплатным" in shown
    assert "telegram:chat=" not in shown


def test_the_checks_reject_the_brief_the_tester_was_actually_shown():
    """The 2026-09-15 brief: expenses from text only, income by /income, no trade-off."""
    shown_that_day = ProductBriefContent.model_validate(
        {
            "summary": "Бот учёта финансов",
            "language": "ru",
            "must_requirements": [
                {"id": "r3", "text": "Распознаёт расходы из текста", "user_wording": _WORDING},
                {"id": "r4", "text": "Записывает доход", "user_wording": _WORDING},
            ],
            "usage_examples": [
                {"requirement_id": "r3", "user_sends": "кофе 250", "product_answers": "Расход"},
                {
                    "requirement_id": "r4",
                    "user_sends": "/income 80000 зарплата",
                    "product_answers": "Записал доход",
                },
            ],
            "initial_settings": [
                {"key": "ocr.method", "value": "free", "description": "Бесплатное распознавание"}
            ],
        }
    )

    assert not income_by_free_text_is_decided(shown_that_day)
    assert not ocr_trade_off_is_named(shown_that_day)
