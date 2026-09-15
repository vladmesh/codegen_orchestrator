"""The finance-bot plan checks read a real model's Russian as well as the scripted English.

On 2026-09-15 `openai/gpt-5.6-sol` wrote the ask-back rule as «спрашивает
пользователя, что тот имел в виду», and the checks, which knew only
«переспрос/уточн», failed a compliant plan. A description that stores
unrecognized input without asking back must still fail.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.unit.architect_finance_bot import _QA_ACTION, assert_every_task_asks_back
from tests.unit.po.finance_bot_replay import _FREE_TEXT, _INCOME


def _tasks(description: str) -> SimpleNamespace:
    return SimpleNamespace(task_payloads=[{"title": "Учёт финансов", "description": description}])


@pytest.mark.parametrize(
    "description",
    [
        pytest.param(
            "бот никогда не должен сохранять нераспознанный ввод как финансовую операцию другого "
            "типа: если смысл сообщения не распознан однозначно, бот спрашивает пользователя, что "
            "тот имел в виду",
            id="production-undefined",
        ),
        pytest.param(
            "Продукт никогда не сохраняет нераспознанный или неоднозначно распознанный ввод как "
            "другой вид записи: он спрашивает пользователя, что тот имел в виду",
            id="production-example",
        ),
        "Если сообщение не удалось распознать, бот спросит пользователя, что он имел в виду.",
        "Нераспознанный ввод не сохраняется: нужно спросить пользователя, расход это или доход.",
        "Если ввод неоднозначный, бот задаёт уточняющий вопрос.",
        "Если бот не понял сообщение, он задаёт пользователю вопрос, что имелось в виду.",
        "Нераспознанное сообщение бот не записывает, а переспрашивает пользователя.",
        "Если сумма не распознана, бот уточняет у пользователя, что тот имел в виду.",
        "Непонятный ввод не сохраняется: бот просит пользователя пояснить.",
        "The bot never stores input it does not recognize as another record: it asks the user "
        "back what they meant.",
        "Unrecognized input is never stored; the bot asks for clarification.",
    ],
)
def test_a_task_carrying_the_ask_back_rule_passes(description):
    assert_every_task_asks_back(_tasks(description))


@pytest.mark.parametrize(
    "description",
    [
        "Нераспознанный ввод сохраняется как расход, не спрашивая пользователя.",
        "Нераспознанное сообщение записывается как расход без уточнения.",
        "Если ввод не распознан, бот записывает его как расход, не переспрашивая.",
        "Unrecognized input is stored as an expense without asking back.",
        "Бот спрашивает пользователя, какую категорию назначить расходу.",
        "Record an expense from a free-text message and from a receipt photo.",
    ],
)
def test_a_task_without_the_ask_back_rule_fails(description):
    with pytest.raises(AssertionError, match="does not carry the ask-back rule"):
        assert_every_task_asks_back(_tasks(description))


@pytest.mark.parametrize(
    ("line", "matches"),
    [
        ('- Telegram: sending "кофе 250" replies "Записал расход 250 ₽" (requirement x)', True),
        ("- Телеграм: отправка «кофе 250» отвечает «Записал расход» (requirement x)", True),
        ("- В Телеграме нажатие кнопки «Отчёт» показывает итог (requirement x)", True),
        ("- Бот записывает расход (requirement x)", False),
    ],
)
def test_a_criterion_is_stated_through_qa_vocabulary_in_either_script(line, matches):
    assert bool(_QA_ACTION.search(line)) is matches


@pytest.mark.parametrize(
    ("reason", "matches"),
    [
        ("Undefined input: can an income be sent as free text, or only as /income?", True),
        ("Не определено: можно ли отправить доход обычным текстом или только /income?", True),
        ("Не определено: можно ли записать доход в свободной форме, как расход «кофе 250»?", True),
        ("Неясно, принимается ли зарплата в произвольной форме или только командой.", True),
        ("Не определено, в какой валюте учитывать доход.", False),
    ],
)
def test_a_returned_reason_names_the_free_text_income_input(reason, matches):
    assert bool(_INCOME.search(reason) and _FREE_TEXT.search(reason)) is matches
