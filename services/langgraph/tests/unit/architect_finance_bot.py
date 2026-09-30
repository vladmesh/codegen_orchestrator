"""The 2026-09-15 finance-bot brief as the architect plans it, in scripted and real-LLM runs.

The brief the tester confirmed that day showed expenses sent as free text and
income only as `/income`, and said nothing about whether an income can be free
text; the architect planned the narrower version and the bot then saved a
free-text salary as an expense. The checks below are the three facts a
compliant plan carries, and both runs apply them to what the real tools sent to
the stub API: one QA criterion per usage example naming its requirement, the
undefined income form returned instead of narrowed, and the ask-back rule in
every task. A balance criterion is judged from the balance QA reads first:
on 2026-09-17 QA's own records from an earlier round made an absolute balance
check fail a correct bot.
"""

from __future__ import annotations

from collections.abc import Collection
import re
from typing import Literal
from unittest.mock import MagicMock, patch

from shared.contracts.dto.product_brief import ProductBriefContent
from shared.contracts.queues.architect import ArchitectMessage
from tests.unit.factories import make_product_brief, make_repository, make_story
from tests.unit.po.finance_bot_replay import (
    _FREE_TEXT,
    _INCOME,
    TELEGRAM_CHAT_ID,
    income_by_free_text_is_decided,
)
from tests.unit.test_architect_consumer import _FakeBriefBoundary, _FakeRedis

EXPENSE_TEXT = "expense-text"
INCOME = "income"
EXPENSE_PHOTO = "expense-photo"

#: The 2026-09-17 check: after income 5000 and expense 300, /balance answers 4700.
BALANCE_SEQUENCE = "/income 5000, затем текст «кофе 300», затем /balance"
BALANCE_REPLY = "Баланс: 4 700 ₽"

STORY_ID = "story-finance-bot"
PREVIOUS_CRITERIA = "- GET /health returns 200"

_WORDING = (
    "Расходы и доходы записывать обычным текстом, а расходы ещё по фото чеков и скриншотам "
    "из банка."
)
STORY_DESCRIPTION = "Бот для учёта личных финансов: расходы и доходы, расходы по фото чеков."

IncomeFreeText = Literal["undefined", "example", "refused"]


def finance_bot_brief(income_free_text: IncomeFreeText) -> ProductBriefContent:
    """The confirmed brief; `income_free_text` says whether it settles free-text income.

    `undefined` is the brief of 2026-09-15. `example` adds a usage example of an
    income sent as free text, `refused` a limitation saying it is not supported.
    """
    usage_examples = [
        {
            "requirement_id": EXPENSE_TEXT,
            "user_sends": "текст «кофе 250»",
            "product_answers": "Записал расход 250 ₽",
        },
        {
            "requirement_id": INCOME,
            "user_sends": "/income 80000 зарплата",
            "product_answers": "Записал доход 80 000 ₽",
        },
        {
            "requirement_id": INCOME,
            "user_sends": BALANCE_SEQUENCE,
            "product_answers": BALANCE_REPLY,
        },
        {
            "requirement_id": EXPENSE_PHOTO,
            "user_sends": "фото чека",
            "product_answers": "Записал расход 1 240 ₽ по чеку",
        },
        {
            "requirement_id": EXPENSE_PHOTO,
            "user_sends": "скриншот операции из банка",
            "product_answers": "Записал расход 3 500 ₽ по скриншоту",
        },
    ]
    limitations = [
        "Чеки распознаются бесплатным способом, поэтому мятый или размытый чек может "
        "прочитаться с ошибками."
    ]
    if income_free_text == "example":
        usage_examples.insert(
            2,
            {
                "requirement_id": INCOME,
                "user_sends": "текст «зарплата 80000»",
                "product_answers": "Записал доход 80 000 ₽",
            },
        )
    elif income_free_text == "refused":
        limitations.append("Доход обычным текстом не поддерживается: только командой /income.")
    content = ProductBriefContent(
        summary="Бот записывает мои расходы и доходы и расходы по фото чеков.",
        language="ru",
        must_requirements=[
            {"id": EXPENSE_TEXT, "text": "Записывает расход из текста", "user_wording": _WORDING},
            {"id": INCOME, "text": "Записывает доход", "user_wording": _WORDING},
            {
                "id": EXPENSE_PHOTO,
                "text": "Записывает расход по фото чека или скриншоту из банка",
                "user_wording": _WORDING,
            },
        ],
        usage_examples=usage_examples,
        limitations=limitations,
    )
    assert income_by_free_text_is_decided(content) is (income_free_text != "undefined")
    return content


class FinanceBotApi(_FakeBriefBoundary):
    """The released brief boundary in memory, plus what the plan wrote outside it."""

    def __init__(self, content: ProductBriefContent):
        super().__init__(make_product_brief(story_id=STORY_ID, content=content))
        self.content = content
        self.task_payloads: list[dict] = []
        self.criteria: str | None = None
        #: `po:input` as the consumer publishes to it.
        self.redis = _FakeRedis()

    async def get_story(self, story_id):
        return make_story(
            id=story_id,
            status="created",
            title="Бот личных финансов",
            description=STORY_DESCRIPTION,
        )

    async def create_task(self, task_data):
        self.task_payloads.append(task_data)
        return await super().create_task(task_data)

    async def get_primary_repository(self, project_id):
        return make_repository(acceptance_criteria=self.criteria or PREVIOUS_CRITERIA)

    async def update_repository(self, repo_id, data):
        self.criteria = data["acceptance_criteria"]
        return make_repository(id=repo_id, acceptance_criteria=self.criteria)


async def plan_finance_bot(api: FinanceBotApi, *, model: str, base_url: str, api_key: str) -> dict:
    """Run the architect consumer on the brief, with `api` behind the consumer and every tool."""
    settings = MagicMock(
        architect_llm_model=model,
        architect_llm_base_url=base_url,
        architect_llm_api_key=api_key,
    )
    job = ArchitectMessage(
        story_id=STORY_ID,
        project_id=str(api.brief.project_id),
        telegram_chat_id=TELEGRAM_CHAT_ID,
    ).model_dump(mode="json")
    with (
        patch("src.consumers.architect.api_client", api),
        patch("src.agents.architect.tools.api_client", api),
        patch("src.consumers.architect.get_settings", return_value=settings),
    ):
        from src.consumers.architect import process_architect_job

        return await process_architect_job(job, api.redis)


_QA_ACTION = re.compile(r"telegram|телеграм|\bGET /|FIRE JOB|button|кнопк", re.IGNORECASE)
_UPLOAD = re.compile(r"фото|скриншот|photo|screenshot|file|файл|upload", re.IGNORECASE)
_NOT_QA_VERIFIABLE = re.compile(r"not\s+QA[\s-]verifiable", re.IGNORECASE)
_READ_OBSERVABLE = re.compile(r"\bGET /")
_TELEGRAM = re.compile(r"telegram|телеграм", re.IGNORECASE)
#: An ask-back form, unless negated: «не спрашивая», «без уточнения», "without asking back"
#: describe storing without asking.
_ASK_BACK = re.compile(
    r"(?<!without\s)(?<!never\s)(?:\bask(?:s|ing)?\b[^.\n]*\bback\b|\basks?\s+the\s+user\b)"
    r"|clarif"
    r"|(?<!\w)(?<!не\s)(?<!без\s)(?:переспрос|переспраш|уточн|спрашива|спрос(?:ит|ить|ят|ив)\b"
    r"|зада(?:[её]т|ст|ть)\s+(?:\w+\s+){0,2}вопрос|проси(?:т|ть)\s+пользовател)",
    re.IGNORECASE,
)
_UNRECOGNIZED = re.compile(
    r"unrecogni[sz]|not\s+recogni[sz]|(?:does\s+not|doesn't|cannot|can't)\s+recogni[sz]"
    r"|ambiguous|не\s+распозна|нераспозна|неоднозначн|непонятн|не\s+понят|не\s+понял"
    r"|не\s+(?:удалось|удаётся|удается|может|смог\w*|получилось)\s+(?:\w+\s+)?распозна",
    re.IGNORECASE,
)


_BALANCE = re.compile(r"баланс|balance", re.IGNORECASE)
_FROM_START = re.compile(r"\bstart|начальн|исходн|стартов", re.IGNORECASE)


def _names(requirement_id: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(requirement_id)}(?![\w-])")


def returned_requirements(api: FinanceBotApi) -> dict[str, str]:
    return {rid: reason for rid, (_, _, reason) in api.coverage.items() if reason}


def assert_one_criterion_per_usage_example(
    api: FinanceBotApi, *, unreachable: Collection[str] = ()
) -> None:
    """Every example of a planned requirement is its own QA line naming that requirement.

    An example whose sending is an upload is sent by QA over Telegram (the QA
    capability catalogue offers a photo or file), checked through a GET of its
    observable, or carries the `not QA-verifiable` marker, but it has a line. The
    examples of a returned requirement have none: nothing builds them yet.

    `unreachable` holds the `user_sends` of examples whose precondition central
    QA's one fixed identity cannot reach — an empty history, "no operations
    yet", another calendar month. Such an example owes no line of its own,
    because the rewrite may collapse it into the check a sibling example
    already carries; a planned requirement still owes at least one criterion,
    so this is never a licence to drop its examples.
    """
    assert api.criteria is not None, "update_acceptance_criteria was never called"
    lines = [line.strip() for line in api.criteria.splitlines() if line.strip().startswith("- ")]
    returned = returned_requirements(api)
    for requirement in api.content.must_requirements:
        if requirement.id in returned:
            continue
        all_examples = [e for e in api.content.usage_examples if e.requirement_id == requirement.id]
        examples = [e for e in all_examples if e.user_sends not in unreachable]
        naming = [line for line in lines if _names(requirement.id).search(line)]
        assert len(naming) >= len(examples), (
            f"requirement {requirement.id} has {len(examples)} usage example(s) but "
            f"{len(naming)} criterion line(s) name it:\n{api.criteria}"
        )
        assert naming or not all_examples, (
            f"requirement {requirement.id} is planned and has usage example(s) but no "
            f"criterion line names it:\n{api.criteria}"
        )
        for line in naming:
            assert _QA_ACTION.search(line) or _NOT_QA_VERIFIABLE.search(line), (
                f"criterion for {requirement.id} is not stated through QA's vocabulary: {line}"
            )
        if any(_UPLOAD.search(example.user_sends) for example in examples):
            assert any(
                _NOT_QA_VERIFIABLE.search(line)
                or _READ_OBSERVABLE.search(line)
                or _TELEGRAM.search(line)
                for line in naming
            ), f"upload example of {requirement.id} is neither sent, observed nor marked:\n{naming}"


def assert_balance_is_judged_from_its_start(api: FinanceBotApi) -> None:
    """A planned balance example is checked as a change from the balance QA reads first.

    QA always acts as one identity whose earlier records stay in the product, so
    an absolute balance fails every retest of a correct bot.
    """
    assert api.criteria is not None, "update_acceptance_criteria was never called"
    returned = returned_requirements(api)
    planned = [
        example
        for example in api.content.usage_examples
        if example.requirement_id not in returned and _BALANCE.search(example.product_answers)
    ]
    if not planned:
        return
    lines = [line.strip() for line in api.criteria.splitlines() if line.strip().startswith("- ")]
    for example in planned:
        balance = [
            line
            for line in lines
            if _names(example.requirement_id).search(line) and _BALANCE.search(line)
        ]
        assert balance, f"no balance criterion for {example.requirement_id}:\n{api.criteria}"
        for line in balance:
            assert _FROM_START.search(line), (
                f"balance criterion is not judged from a starting value QA reads first: {line}"
            )


def assert_undefined_income_is_returned(api: FinanceBotApi) -> None:
    """Only an income form the brief leaves undefined is returned, and the reason names it."""
    must = {requirement.id for requirement in api.content.must_requirements}
    assert set(api.coverage) == must, f"undisposed requirements: {must - set(api.coverage)}"
    returned = returned_requirements(api)
    if income_by_free_text_is_decided(api.content):
        assert returned == {}, f"returned although the brief settles the input: {returned}"
        return
    assert INCOME in returned, (
        f"{INCOME} was planned narrower instead of returned: coverage {api.coverage[INCOME]}"
    )
    reason = returned[INCOME]
    assert _INCOME.search(reason) and _FREE_TEXT.search(reason), (
        f"the returned reason does not name the undefined free-text income input: {reason}"
    )
    assert set(returned) == {INCOME}, f"returned more than the undefined input: {returned}"


def assert_every_task_asks_back(api: FinanceBotApi) -> None:
    """Every task requires asking back instead of storing unrecognized input as another record."""
    assert api.task_payloads, "the plan created no task"
    for task in api.task_payloads:
        description = task["description"]
        assert _ASK_BACK.search(description) and _UNRECOGNIZED.search(description), (
            f"task {task['title']!r} does not carry the ask-back rule: {description}"
        )


def assert_the_owner_is_told_what_was_returned(api: FinanceBotApi) -> None:
    """One `story_requirements_returned` event names every returned requirement and its reason."""
    events = [
        fields
        for _, fields in api.redis.published
        if fields["event"] == "story_requirements_returned"
    ]
    returned = returned_requirements(api)
    if not returned:
        assert events == [], f"notice published although nothing was returned: {events}"
        return
    assert len(events) == 1, f"expected one returned-requirements event, got {events}"
    event = events[0]
    assert event["telegram_chat_id"] == TELEGRAM_CHAT_ID and event["story_id"] == STORY_ID
    requirements = {r.id: r for r in api.content.must_requirements}
    for requirement_id, reason in returned.items():
        assert f"- {requirement_id}: {requirements[requirement_id].text}" in event["text"]
        assert f"reason: {reason}" in event["text"]
    assert _WORDING in event["text"]


def assert_plan_uses_exactly_the_confirmed_examples(api: FinanceBotApi) -> None:
    assert_one_criterion_per_usage_example(api)
    assert_balance_is_judged_from_its_start(api)
    assert_undefined_income_is_returned(api)
    assert_every_task_asks_back(api)
