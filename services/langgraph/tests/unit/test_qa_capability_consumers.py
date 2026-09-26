"""Tripwire: every consumer of QA's capabilities reads them from the one catalogue.

Four texts tell a model, or a filter, what QA can do: the Architect's
instructions and tools, the PO's brief tool as the model sees it, the QA
executor's prompt and instruction file, and the pre-QA criteria filter. Each is
rendered here exactly as it is handed over. The catalogue's generated block must
be in it, and once that block is cut out, nothing the catalogue words — nor a
phrase from the hand-written lists it replaced — may be left anywhere else. A
second, hand-kept list drifts from the sandbox; this is where it is caught.
"""

from __future__ import annotations

import inspect
import json

from langchain_core.utils.function_calling import convert_to_openai_tool
import pytest

from shared.contracts.qa_capabilities import QA_ACTIONS, QA_NEVER, http_write_methods
from src.agents.architect.tools import get_architect_tools
from src.agents.po.tools import get_all_tools
from src.agents.qa import acceptance
from src.prompts.architect import SYSTEM_PROMPT as ARCHITECT_PROMPT
from src.prompts.po import SYSTEM_PROMPT as PO_PROMPT
from src.prompts.qa import build_qa_instructions, build_qa_prompt
from src.prompts.qa_capabilities import (
    render_architect_capabilities,
    render_brief_capabilities,
    render_executor_capabilities,
)

#: Phrases of the hand-written lists the catalogue replaced. Each one either
#: restated QA's actions by hand or claimed an upload is beyond it.
RETIRED_PHRASES = (
    "never uploads",
    "closed set of read-only actions",
    "inline button press",
    "a Telegram text message and its reply",
    "a Telegram text message sent to the bot",
    "photo upload",
    "upload step",
    "never as a POST",
    "the calls above cannot",
    "your tools cannot perform",
    "it reaches nothing",
    "never hold the account's credentials",
)


def _tool_texts(tools: list) -> str:
    """Each tool as the model receives it: its description, then its argument schema."""
    functions = [convert_to_openai_tool(t)["function"] for t in tools]
    return "\n".join(
        f"{f['description']}\n{json.dumps(f['parameters'], ensure_ascii=False)}" for f in functions
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


def _architect() -> tuple[str, str]:
    return ARCHITECT_PROMPT + "\n" + _tool_texts(get_architect_tools()), (
        render_architect_capabilities()
    )


def _po() -> tuple[str, str]:
    return PO_PROMPT + "\n" + _tool_texts(get_all_tools()), (
        render_brief_capabilities(indent=" " * 12)
    )


def _qa() -> tuple[str, str]:
    prompt = build_qa_prompt(
        "- Telegram: sending a location replies with the nearest shop",
        "https://shop.example.com",
        bot_username="shop_bot",
    )
    return prompt + "\n" + build_qa_instructions(), render_executor_capabilities(telegram=True)


def _acceptance() -> tuple[str, str]:
    source = inspect.getsource(acceptance)
    block = "_HTTP_WRITES = http_write_methods()"
    return source, block


CONSUMERS = {
    "architect": _architect,
    "po": _po,
    "qa": _qa,
    "acceptance": _acceptance,
}


def _remainder(text: str, block: str) -> str:
    assert block in text, "the catalogue's generated block is missing"
    return text.replace(block, "")


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_the_block_is_there_and_nothing_else_lists_what_qa_can_do(consumer):
    text, block = CONSUMERS[consumer]()
    rest = _flat(_remainder(text, block))
    wordings = [a.wording for a in QA_ACTIONS] + [n.wording for n in QA_NEVER]

    leaked = [w for w in (*wordings, *RETIRED_PHRASES) if _flat(w).lower() in rest.lower()]

    assert leaked == []


def test_the_po_tool_schema_the_model_sees_carries_the_rendered_guidance():
    brief = next(t for t in get_all_tools() if t.name == "present_product_brief")
    description = convert_to_openai_tool(brief)["function"]["description"]

    assert render_brief_capabilities(indent=" " * 12) in description


def test_the_pre_qa_filter_withholds_exactly_the_catalogues_http_writes():
    assert acceptance._HTTP_WRITES == http_write_methods()
    source = inspect.getsource(acceptance)
    for method in http_write_methods():
        assert f'"{method}"' not in source, method


def test_the_block_is_rendered_only_from_the_catalogue():
    """Every offered action appears in each prompt block; the web platform in none."""
    architect = _flat(render_architect_capabilities())
    executor = _flat(render_executor_capabilities(telegram=True))
    brief = _flat(render_brief_capabilities(indent=""))

    for action in QA_ACTIONS:
        assert action.wording in executor
        if action.criterion:
            assert action.wording in architect
            assert action.wording in brief
    for never in QA_NEVER:
        assert never.wording in architect and never.wording in executor
    assert "Web:" not in architect and "Web:" not in executor
