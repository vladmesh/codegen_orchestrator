"""A scripted replay of the 2026-09-15 finance-bot dialogue through the real PO graph.

The model is scripted to make the `present_product_brief` call a PO following
the prompt makes; everything past the model is real — the graph the consumer
builds, its tool node, the tool, its validation and its render. What is asserted
is the brief the tool stored and the message the user is shown, with the same
checks the opt-in real-LLM replay (`tests/e2e/test_po_finance_bot_brief.py`)
applies to a live model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from shared.contracts.dto.product_brief import ProductBriefContent
from shared.contracts.queues.po import MESSAGE_BREAK
from src.agents.po.graph import create_po_graph
from src.agents.po.tools_shared import init_po_clients
from tests.unit.po.finance_bot_replay import (
    TELEGRAM_CHAT_ID,
    USER_MESSAGES,
    income_by_free_text_is_decided,
    ocr_trade_off_is_named,
    user_message,
)
from tests.unit.po.test_tools_briefs import _API, BRIEF_ID, PROJECT_ID, _brief
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

    graph = await create_po_graph(llm=model, summarization_llm=model)
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
    assert "• Вы отправляете: текст «зарплата 80000»" in shown
    assert "<b>Ограничения</b>\n• Чеки распознаются бесплатным" in shown
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


def _brief_with_limitation(limitation: str) -> ProductBriefContent:
    return ProductBriefContent.model_validate(
        {
            "summary": "Бот учёта финансов",
            "language": "ru",
            "must_requirements": [
                {"id": "r4", "text": "Записывает доход", "user_wording": _WORDING},
            ],
            "usage_examples": [
                {
                    "requirement_id": "r4",
                    "user_sends": "/income 80000 зарплата",
                    "product_answers": "Записал доход",
                },
            ],
            "limitations": [limitation],
        }
    )


@pytest.mark.parametrize(
    "limitation",
    [
        "Доходы пока только в рублях.",
        "Учёт доходов только базовый.",
        "Income is tracked in one currency only.",
        "Нельзя экспортировать доходы в Excel.",
    ],
)
def test_a_vague_income_limitation_does_not_decide_free_text_income(limitation):
    assert not income_by_free_text_is_decided(_brief_with_limitation(limitation))


@pytest.mark.parametrize(
    "limitation",
    [
        "Доход нельзя записать обычным текстом — только командой /income.",
        "Бот не распознаёт доходы из свободного текста, используйте /income.",
        "Income cannot be added as free text; use /income.",
    ],
)
def test_an_explicit_refusal_of_free_text_income_decides_it(limitation):
    assert income_by_free_text_is_decided(_brief_with_limitation(limitation))


@pytest.mark.asyncio
async def test_the_full_brief_is_the_turns_answer_with_its_message_breaks():
    """`show_full_brief` ends the turn, so its breaks reach the bot as written.

    The consumer sends the last message of the turn. A model asked to relay a
    text with control characters in it would not reproduce them; the tool's own
    result, returned directly, does.
    """
    api = _API(briefs={BRIEF_ID: _brief()})
    init_po_clients(api, AsyncMock())
    show = {"name": "show_full_brief", "args": {"brief_id": BRIEF_ID}, "id": "call-full-1"}
    model = _ScriptedToolCallingModel(
        turns=[AIMessage(content="", tool_calls=[show]), AIMessage(content="re-typed text")]
    )
    config = {"configurable": {"thread_id": "po-full-brief", "telegram_chat_id": TELEGRAM_CHAT_ID}}
    graph = await create_po_graph(llm=model, summarization_llm=model)

    state = await graph.ainvoke(
        {"messages": [HumanMessage(content="Show me the whole brief")]}, config=config
    )

    last = state["messages"][-1]
    assert isinstance(last, ToolMessage)
    assert last.name == "show_full_brief"
    assert MESSAGE_BREAK in last.content
    assert last.content.split(MESSAGE_BREAK)[1].startswith("<b>What you get</b>")
