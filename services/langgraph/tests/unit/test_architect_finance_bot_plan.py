"""A scripted architect plans the 2026-09-15 finance-bot brief through the real graph and tools.

The model is scripted to make the calls an architect following the prompt
makes; everything past it is real — the consumer, its claim and briefing, the
graph, its tool node, the tools and the admission. What is asserted is what the
tools sent to the stub API, with the checks the opt-in real-LLM run
(`tests/e2e/test_architect_finance_bot_plan.py`) applies to a live model. The
counterfactual scripts show the checks fail on a plan that narrows silently,
drops an example or forgets the ask-back rule.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
from unittest.mock import patch

from langchain_core.messages import AIMessage, BaseMessage
import pytest

from src.agents.architect.tools import reset_task_chain
from tests.unit.architect_finance_bot import (
    BALANCE_REPLY,
    BALANCE_SEQUENCE,
    EXPENSE_PHOTO,
    EXPENSE_TEXT,
    INCOME,
    PREVIOUS_CRITERIA,
    FinanceBotApi,
    IncomeFreeText,
    assert_plan_uses_exactly_the_confirmed_examples,
    assert_the_owner_is_told_what_was_returned,
    finance_bot_brief,
    plan_finance_bot,
)
from tests.unit.test_architect_graph import _ScriptedToolCallingModel

ASK_BACK_RULE = (
    "The bot never stores input it does not recognize as a different kind of record: it asks "
    "the user back what they meant."
)
UNDEFINED_INCOME_REASON = (
    "Undefined input: can an income be sent as free text like the expense example «кофе 250», "
    "or only as /income? No usage example and no limitation says."
)


class _RecordingScriptedModel(_ScriptedToolCallingModel):
    """The scripted model, keeping what it was asked so the briefing can be asserted."""

    seen: list[list[BaseMessage]] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@dataclass(frozen=True)
class _Plan:
    """What the scripted architect does with the brief."""

    return_income: bool
    drop_photo_criterion: bool = False
    ask_back: bool = True
    absolute_balance: bool = False


def _script(api: FinanceBotApi, plan: _Plan) -> list[AIMessage]:
    ids = (f"call-{n}" for n in itertools.count(1))
    ask_back = f" {ASK_BACK_RULE}" if plan.ask_back else ""

    def turn(*calls: tuple[str, dict]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": next(ids)} for name, args in calls],
        )

    def task(title: str, description: str) -> tuple[str, dict]:
        return (
            "create_task",
            {
                "title": title,
                "description": description + ask_back,
                "type": "feature",
                "acceptance_criteria": f"{description}{ask_back}",
                "story_id": api.brief.story_id,
                "project_id": str(api.brief.project_id),
            },
        )

    turns = [
        turn(
            task(
                "Expenses from text and receipt photos",
                "Record an expense from a free-text message and from a receipt photo or bank "
                "screenshot with the free recognition.",
            )
        )
    ]
    coverage = [
        ("record_requirement_coverage", {"requirement_id": EXPENSE_TEXT, "task_id": "task-1"}),
        ("record_requirement_coverage", {"requirement_id": EXPENSE_PHOTO, "task_id": "task-1"}),
    ]
    if plan.return_income:
        coverage.append(
            (
                "record_requirement_coverage",
                {"requirement_id": INCOME, "returned_reason": UNDEFINED_INCOME_REASON},
            )
        )
    else:
        turns.append(turn(task("Incomes", "Record an income as the brief shows it.")))
        coverage.append(
            ("record_requirement_coverage", {"requirement_id": INCOME, "task_id": "task-2"})
        )
    turns.append(turn(*coverage))

    criteria = [PREVIOUS_CRITERIA]
    for example in api.content.usage_examples:
        if plan.return_income and example.requirement_id == INCOME:
            continue
        if example.requirement_id == EXPENSE_PHOTO:
            if not plan.drop_photo_criterion:
                criteria.append(
                    f"- GET /api/expenses lists the expense recorded from «{example.user_sends}»; "
                    "the upload is not QA-verifiable: needs a photo upload "
                    f"(requirement {EXPENSE_PHOTO})"
                )
            continue
        if example.user_sends == BALANCE_SEQUENCE:
            if plan.absolute_balance:
                criteria.append(
                    '- Telegram: after "/income 5000" and "кофе 300", "/balance" replies '
                    f'"{BALANCE_REPLY}" (requirement {INCOME})'
                )
            else:
                criteria.append(
                    '- Telegram: send "/balance" and note the starting balance, send '
                    '"/income 5000" and "кофе 300"; "/balance" then replies '
                    f'"Баланс: <start + 4700> ₽" (requirement {INCOME})'
                )
            continue
        sent = example.user_sends.split("«")[-1].rstrip("»")
        criteria.append(
            f'- Telegram: sending "{sent}" replies "{example.product_answers}" '
            f"(requirement {example.requirement_id})"
        )
    update = {"project_id": str(api.brief.project_id), "acceptance_criteria": "\n".join(criteria)}
    turns.append(turn(("update_acceptance_criteria", update)))
    turns.append(AIMessage(content="The plan is recorded."))
    return turns


async def _run(income_free_text: IncomeFreeText, plan: _Plan) -> tuple[FinanceBotApi, dict, list]:
    api = FinanceBotApi(finance_bot_brief(income_free_text))
    model = _RecordingScriptedModel(turns=_script(api, plan), seen=[])
    reset_task_chain()
    with patch("src.agents.architect.graph.ChatOpenAI", return_value=model):
        result = await plan_finance_bot(
            api, model="scripted", base_url="http://llm.invalid", api_key="x"
        )
    reset_task_chain()
    return api, result, model.seen


@pytest.mark.asyncio
async def test_the_undefined_income_form_is_returned_and_every_example_is_a_check():
    api, result, seen = await _run("undefined", _Plan(return_income=True))

    assert result["status"] == "success", result
    assert api.admit_calls == 1 and api.released == ["task-1"]
    assert_plan_uses_exactly_the_confirmed_examples(api)
    # The returned free-text income form is told to the owner, with its reason.
    assert_the_owner_is_told_what_was_returned(api)
    assert len(api.redis.published) == 1
    assert "- income: Записывает доход" in api.redis.published[0][1]["text"]
    assert UNDEFINED_INCOME_REASON in api.redis.published[0][1]["text"]
    # The examples reached the model in the user's words, grouped by requirement.
    briefing = seen[0][-1].content
    assert f"[{INCOME}]\n  - the user sends: /income 80000 зарплата" in briefing
    assert "Чеки распознаются бесплатным способом" in briefing


@pytest.mark.asyncio
@pytest.mark.parametrize("income_free_text", ["example", "refused"])
async def test_a_brief_that_settles_free_text_income_returns_nothing(income_free_text):
    api, result, _ = await _run(income_free_text, _Plan(return_income=False))

    assert result["status"] == "success", result
    assert api.released == ["task-1", "task-2"]
    assert_plan_uses_exactly_the_confirmed_examples(api)
    assert_the_owner_is_told_what_was_returned(api)
    assert api.redis.published == []
    # The 2026-09-17 balance check is written against the balance QA reads first.
    balance = [line for line in api.criteria.splitlines() if "/balance" in line]
    assert len(balance) == 1 and "<start + 4700>" in balance[0], api.criteria


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("income_free_text", "plan", "failure"),
    [
        pytest.param(
            "undefined", _Plan(return_income=False), "planned narrower", id="narrows-silently"
        ),
        pytest.param(
            "refused", _Plan(return_income=True), "returned although", id="returns-a-settled-input"
        ),
        pytest.param(
            "undefined",
            _Plan(return_income=True, drop_photo_criterion=True),
            f"requirement {EXPENSE_PHOTO} has 2 usage example",
            id="drops-the-upload-example",
        ),
        pytest.param(
            "undefined",
            _Plan(return_income=True, ask_back=False),
            "does not carry the ask-back rule",
            id="forgets-the-ask-back-rule",
        ),
        pytest.param(
            "example",
            _Plan(return_income=False, absolute_balance=True),
            "not judged from a starting value",
            id="checks-an-absolute-balance",
        ),
    ],
)
async def test_the_checks_fail_on_a_plan_that_breaks_a_rule(income_free_text, plan, failure):
    api, result, _ = await _run(income_free_text, plan)

    # The boundary alone cannot tell: every one of these plans is admitted.
    assert result["status"] == "success", result
    with pytest.raises(AssertionError, match=failure):
        assert_plan_uses_exactly_the_confirmed_examples(api)
