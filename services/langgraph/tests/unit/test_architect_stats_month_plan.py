"""A scripted architect replays the 2026-09-17 stats brief through the real graph and tools.

The model is scripted to make the calls an architect following the prompt
makes; everything past it is real — the consumer, its claim and briefing, the
graph, its tool node, the tools and the admission. What is asserted is what the
tools sent to the stub API.

`stats.r2` asks for a state central QA's one fixed identity cannot be in, and
the counterfactual scripts show the checks fail on the plan that parked
`story-f4a3fe1b`: the example copied verbatim, the example silently dropped, a
`/stats` total asserted absolutely, and the reachable example losing its own
criterion.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
from typing import Literal
from unittest.mock import patch

from langchain_core.messages import AIMessage
import pytest

from src.agents.architect.tools import reset_task_chain
from tests.unit.architect_finance_bot import EXPENSE_TEXT, PREVIOUS_CRITERIA, plan_finance_bot
from tests.unit.architect_stats_month import (
    EXPENSE_ANSWERS,
    STATS,
    StatsApi,
    assert_no_criterion_needs_a_state_qa_cannot_reach,
    assert_plan_checks_only_states_qa_can_reach,
    assert_the_read_first_stats_criterion_is_present,
    stats_api,
)
from tests.unit.test_architect_graph import _ScriptedToolCallingModel

ASK_BACK_RULE = (
    "The bot never stores input it does not recognize as a different kind of record: it asks "
    "the user back what they meant."
)

#: The rewrite: `stats.r2` collapses into the read-first check `stats.r1` carries.
READ_FIRST_STATS_CRITERION = (
    '- Telegram: send "/stats" and note the starting Доходы and Расходы, send "/income 1000" '
    'and "кофе 250"; "/stats" then replies "Доходы: <start + 1000> USD / '
    f'Расходы: <start + 250> USD" (requirement {STATS})'
)
#: The line that parked the story on 2026-09-17, copied out of the brief verbatim.
VERBATIM_STATS_CRITERION = (
    '- Telegram: для пользователя без операций в текущем календарном месяце "/stats" '
    f'отвечает "Доходы: 0 USD / Расходы: 0 USD" (requirement {STATS})'
)
#: An absolute total: reachable, but red on every later round of a correct bot.
ABSOLUTE_STATS_CRITERION = (
    '- Telegram: after "/income 1000" and "кофе 250", "/stats" replies '
    f'"Доходы: 1000 USD / Расходы: 250 USD" (requirement {STATS})'
)
EXPENSE_CRITERION = (
    f'- Telegram: sending "кофе 250" replies "{EXPENSE_ANSWERS}" (requirement {EXPENSE_TEXT})'
)

RETURN_REASON = (
    "Unreachable precondition: проверка требует пользователя без операций в текущем "
    "календарном месяце, а единственная фиксированная личность QA их уже накопила и не "
    "может ни сбросить состояние, ни стать вторым пользователем."
)

StatsDisposition = Literal["rewrite", "return", "verbatim", "drop", "absolute"]


@dataclass(frozen=True)
class _Plan:
    """What the scripted architect does with `stats.r2`."""

    stats: StatsDisposition
    drop_expense_criterion: bool = False


def _script(api: StatsApi, plan: _Plan) -> list[AIMessage]:
    ids = (f"call-{n}" for n in itertools.count(1))

    def turn(*calls: tuple[str, dict]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": next(ids)} for name, args in calls],
        )

    description = (
        "Record an expense from a free-text message and answer /stats with the current "
        f"month's income and expense totals. {ASK_BACK_RULE}"
    )
    turns = [
        turn(
            (
                "create_task",
                {
                    "title": "Expenses from text and the monthly stats summary",
                    "description": description,
                    "type": "feature",
                    "acceptance_criteria": description,
                    "story_id": api.brief.story_id,
                    "project_id": str(api.brief.project_id),
                },
            )
        )
    ]

    coverage = [
        ("record_requirement_coverage", {"requirement_id": EXPENSE_TEXT, "task_id": "task-1"})
    ]
    if plan.stats == "return":
        coverage.append(
            (
                "record_requirement_coverage",
                {"requirement_id": STATS, "returned_reason": RETURN_REASON},
            )
        )
    else:
        coverage.append(
            ("record_requirement_coverage", {"requirement_id": STATS, "task_id": "task-1"})
        )
    turns.append(turn(*coverage))

    criteria = [PREVIOUS_CRITERIA]
    if not plan.drop_expense_criterion:
        criteria.append(EXPENSE_CRITERION)
    if plan.stats == "rewrite":
        criteria.append(READ_FIRST_STATS_CRITERION)
    elif plan.stats == "verbatim":
        criteria.append(READ_FIRST_STATS_CRITERION)
        criteria.append(VERBATIM_STATS_CRITERION)
    elif plan.stats == "absolute":
        criteria.append(ABSOLUTE_STATS_CRITERION)
    update = {"project_id": str(api.brief.project_id), "acceptance_criteria": "\n".join(criteria)}
    turns.append(turn(("update_acceptance_criteria", update)))
    turns.append(AIMessage(content="The plan is recorded."))
    return turns


async def _run(plan: _Plan) -> tuple[StatsApi, dict]:
    api = stats_api()
    model = _ScriptedToolCallingModel(turns=_script(api, plan))
    reset_task_chain()
    with patch("src.agents.architect.graph.ChatOpenAI", return_value=model):
        result = await plan_finance_bot(
            api, model="scripted", base_url="http://llm.invalid", api_key="x"
        )
    reset_task_chain()
    return api, result


@pytest.mark.asyncio
async def test_the_unreachable_example_is_rewritten_into_the_read_first_check():
    api, result = await _run(_Plan(stats="rewrite"))

    assert result["status"] == "success", result
    assert api.released == ["task-1"]
    assert_plan_checks_only_states_qa_can_reach(api)
    # `stats.r1` keeps its own read-first criterion, naming its requirement id...
    assert_the_read_first_stats_criterion_is_present(api)
    assert READ_FIRST_STATS_CRITERION in api.criteria
    # ...and `stats.r2` left no zero-total line behind it.
    assert len([line for line in api.criteria.splitlines() if STATS in line]) == 1
    assert "Доходы: 0" not in api.criteria and "Расходы: 0" not in api.criteria
    # The reachable example is still its own criterion.
    assert EXPENSE_CRITERION in api.criteria


@pytest.mark.asyncio
async def test_returning_the_requirement_is_the_other_permitted_disposition():
    api, result = await _run(_Plan(stats="return"))

    assert result["status"] == "success", result
    assert_plan_checks_only_states_qa_can_reach(api)
    assert api.coverage[STATS][2] == RETURN_REASON
    # A returned requirement gets no criterion at all, and the owner is told why.
    assert STATS not in api.criteria
    events = [
        fields
        for _, fields in api.redis.published
        if fields["event"] == "story_requirements_returned"
    ]
    assert len(events) == 1 and RETURN_REASON in events[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "failure"),
    [
        pytest.param(
            _Plan(stats="verbatim"),
            "needs a state QA's one identity cannot reach",
            id="copies-the-unreachable-example-verbatim",
        ),
        pytest.param(
            _Plan(stats="drop"),
            f"no criterion names requirement {STATS}",
            id="drops-the-example-silently",
        ),
        pytest.param(
            _Plan(stats="absolute"),
            "is stated relative to the totals QA reads first",
            id="asserts-an-absolute-total",
        ),
        pytest.param(
            _Plan(stats="rewrite", drop_expense_criterion=True),
            f"requirement {EXPENSE_TEXT} has 1 usage example",
            id="drops-a-reachable-example",
        ),
    ],
)
async def test_the_checks_fail_on_a_plan_that_breaks_a_rule(plan, failure):
    api, result = await _run(plan)

    # The boundary alone cannot tell: every one of these plans is admitted.
    assert result["status"] == "success", result
    with pytest.raises(AssertionError, match=failure):
        assert_plan_checks_only_states_qa_can_reach(api)


@pytest.mark.asyncio
async def test_the_verbatim_line_is_what_parked_the_story():
    """The 2026-09-17 plan: `stats.r1` passed and `stats.r2` failed `qa_capability`."""
    api, _ = await _run(_Plan(stats="verbatim"))

    assert VERBATIM_STATS_CRITERION in api.criteria
    with pytest.raises(AssertionError, match="zero or empty total|cannot reach"):
        assert_no_criterion_needs_a_state_qa_cannot_reach(api)
