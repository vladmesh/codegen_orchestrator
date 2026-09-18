"""The 2026-09-17 stats brief (revision 2) as the architect must plan it.

On 2026-09-17 the extension story `story-f4a3fe1b` parked on its fifth attempt
with a correct product: brief revision 2 carried two usage examples of the
`/stats` requirement, and the architect copied the second one into the
repository criteria verbatim. `stats.r1` — the totals grow by the recorded
income and expense from the observed start — passed. `stats.r2` — "for a user
with no operations in the current calendar month, `/stats` replies `Доходы:
0 USD / Расходы: 0 USD`" — failed with cause `qa_capability`: central QA is one
fixed Telegram identity whose operations from earlier rounds persist, and it
can neither reset that state nor act as a second user.

The checks below are the facts a compliant plan carries, applied to what the
real tools sent to the stub API: no criterion asserts a state that identity
cannot reach, `stats.r2` is either rewritten into the read-first check `stats.r1`
already carries or returned to the PO with a reason naming the unreachable
precondition, and an example whose precondition QA *can* reach still gets its
own criterion naming its requirement id.
"""

from __future__ import annotations

import re

from shared.contracts.dto.product_brief import ProductBriefContent
from tests.unit.architect_finance_bot import (
    EXPENSE_TEXT,
    FinanceBotApi,
    assert_every_task_asks_back,
    assert_one_criterion_per_usage_example,
    returned_requirements,
)
from tests.unit.factories import make_story

STATS = "stats"

STORY_DESCRIPTION = (
    "Расширение бота личных финансов: команда /stats со сводкой доходов и расходов "
    "за текущий месяц."
)

_WORDING = "Хочу командой посмотреть, сколько я в этом месяце заработал и сколько потратил."

#: `stats.r1` — the accumulated-state example the read-first rule already covers.
R1_SENDS = "/stats, затем текст «кофе 250» и /income 1000, затем снова /stats"
R1_ANSWERS = "Доходы и Расходы выросли ровно на записанные 1000 USD и 250 USD"

#: `stats.r2` — the example whose precondition QA's one fixed identity cannot reach.
R2_SENDS = "у пользователя ещё нет операций в текущем календарном месяце, он отправляет /stats"
R2_ANSWERS = "Доходы: 0 USD / Расходы: 0 USD"

#: The one example of this brief whose precondition QA can reach unaided.
EXPENSE_SENDS = "текст «кофе 250»"
EXPENSE_ANSWERS = "Записал расход 250 USD"


def stats_brief() -> ProductBriefContent:
    """Brief revision 2 of 2026-09-17, with both `/stats` usage examples."""
    return ProductBriefContent(
        summary="Бот записывает мои расходы и доходы и показывает сводку за месяц.",
        language="ru",
        must_requirements=[
            {
                "id": EXPENSE_TEXT,
                "text": "Записывает расход из текста",
                "user_wording": "Расходы записывать обычным текстом.",
            },
            {
                "id": STATS,
                "text": "Показывает суммы доходов и расходов за текущий месяц",
                "user_wording": _WORDING,
            },
        ],
        usage_examples=[
            {
                "requirement_id": EXPENSE_TEXT,
                "user_sends": EXPENSE_SENDS,
                "product_answers": EXPENSE_ANSWERS,
            },
            {"requirement_id": STATS, "user_sends": R1_SENDS, "product_answers": R1_ANSWERS},
            {"requirement_id": STATS, "user_sends": R2_SENDS, "product_answers": R2_ANSWERS},
        ],
        limitations=["Сводка считается только за текущий календарный месяц."],
    )


class StatsApi(FinanceBotApi):
    """The finance-bot stub behind the extension story that adds `/stats`."""

    async def get_story(self, story_id):
        return make_story(
            id=story_id,
            status="created",
            title="Сводка доходов и расходов",
            description=STORY_DESCRIPTION,
        )


def stats_api() -> StatsApi:
    return StatsApi(stats_brief())


#: A total stated as an absolute zero or an empty list — what only a state QA
#: cannot reach would answer.
_ZERO_OR_EMPTY = re.compile(
    r"(?:^|[\s:=«\"'(\[])0(?:[.,]0+)?(?![\d.,])|\bzero\b|\bнул(?:ь|я|ю|ём|ем|ев\w*)\b"
    r"|\bempty\b|\bпуст\w*",
    re.IGNORECASE,
)
#: A precondition QA's one fixed identity cannot be in, in either script.
_UNREACHABLE_PRECONDITION = re.compile(
    r"нет\s+операц|без\s+операц|не\s+было\s+операц|ни\s+одной\s+операц|пуст\w*\s+истор"
    r"|нов\w+\s+пользовател|друг\w+\s+пользовател|друг\w+\s+месяц|прошл\w+\s+месяц"
    r"|no\s+operations|no\s+records\s+yet|empty\s+history|fresh\s+user|new\s+user"
    r"|another\s+(?:user|month)|previous\s+month|недостижим|unreachable",
    re.IGNORECASE,
)
#: The check is stated relative to what QA reads before it acts.
_READ_FIRST = re.compile(
    r"\bstart|\bfirst\b|сначала|начальн|исходн|стартов|текущ\w+\s+значен|вырос|увелич|прирос",
    re.IGNORECASE,
)
_STATS_OBSERVABLE = re.compile(r"/stats", re.IGNORECASE)


def _criteria_lines(api: StatsApi) -> list[str]:
    assert api.criteria is not None, "update_acceptance_criteria was never called"
    return [line.strip() for line in api.criteria.splitlines() if line.strip().startswith("- ")]


def _lines_naming(api: StatsApi, requirement_id: str) -> list[str]:
    names = re.compile(rf"(?<![\w-]){re.escape(requirement_id)}(?![\w-])")
    return [line for line in _criteria_lines(api) if names.search(line)]


def assert_no_criterion_needs_a_state_qa_cannot_reach(api: StatsApi) -> None:
    """No criterion asserts an absolute zero total or names an unreachable precondition.

    This is exactly the line that parked `story-f4a3fe1b`: QA's fixed identity
    already had current-month operations, so a zero total is red on a correct
    product and its `qa_capability` failure parks the story.
    """
    for line in _criteria_lines(api):
        assert not _UNREACHABLE_PRECONDITION.search(line), (
            f"a criterion needs a state QA's one identity cannot reach: {line}"
        )
        assert not _ZERO_OR_EMPTY.search(line), (
            f"a criterion asserts a zero or empty total QA cannot produce: {line}"
        )


def assert_the_read_first_stats_criterion_is_present(api: StatsApi) -> None:
    """`stats.r1` is checked through `/stats`, relative to what QA reads first, naming `stats`."""
    naming = _lines_naming(api, STATS)
    assert naming, f"no criterion names requirement {STATS}:\n{api.criteria}"
    read_first = [
        line for line in naming if _STATS_OBSERVABLE.search(line) and _READ_FIRST.search(line)
    ]
    assert read_first, (
        f"no {STATS} criterion is stated relative to the totals QA reads first:\n{naming}"
    )


def assert_the_unreachable_example_is_rewritten_or_returned(api: StatsApi) -> None:
    """`stats.r2` gets one of the two permitted dispositions, and never a silent drop."""
    returned = returned_requirements(api)
    if STATS in returned:
        reason = returned[STATS]
        assert _UNREACHABLE_PRECONDITION.search(reason), (
            f"the returned reason does not name the unreachable precondition: {reason}"
        )
        assert not _lines_naming(api, STATS), (
            f"requirement {STATS} was returned but still carries a criterion:\n{api.criteria}"
        )
        return
    assert_the_read_first_stats_criterion_is_present(api)


def assert_a_reachable_example_keeps_its_own_criterion(api: StatsApi) -> None:
    """The rewrite frees only `stats.r2`: every reachable example still owes its own line."""
    assert_one_criterion_per_usage_example(api, unreachable=(R2_SENDS,))


def assert_plan_checks_only_states_qa_can_reach(api: StatsApi) -> None:
    assert_no_criterion_needs_a_state_qa_cannot_reach(api)
    assert_the_unreachable_example_is_rewritten_or_returned(api)
    assert_a_reachable_example_keeps_its_own_criterion(api)
    assert_every_task_asks_back(api)
