"""The customer's words, and only the customer's words.

A scripted LLM persona reads what the Codegen bot said and answers as a product-only
customer: it wants a bot that sends it posts from named public channels, chooses
Russian, accepts the product brief and a proposed split into stages. It is asked
only after the order's project is proven and admitted to the module rollout, and
it decides nothing about the operation. The controller owns credentials, buttons, waits and
every lifecycle decision; the persona's structured answer is a proposal the
controller checks before anything is sent.

The persona never sees a secret: a message that carried the promo code or the
product token appears in its transcript as a placeholder, and the controller
sends those values itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import re
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Scenario


class PersonaDecision(StrEnum):
    #: Send `text` to the Codegen bot.
    REPLY = "reply"
    #: Press the visible button whose label is `button`.
    PRESS = "press"
    #: The bot is still working, or said nothing that needs an answer.
    WAIT = "wait"
    #: The bot said it cannot do what the customer wants.
    IMPOSSIBLE = "impossible"


class PersonaTurn(BaseModel):
    """One structured answer of the persona: words or a button, never an authority.

    Whether a reply may cross the confirmation boundary is the controller's
    decision from API evidence; nothing the model says about its own answer counts.
    """

    model_config = ConfigDict(extra="forbid")

    decision: PersonaDecision
    text: str | None = Field(default=None, max_length=1500)
    button: str | None = Field(default=None, max_length=200)


class PersonaInvalid(RuntimeError):  # noqa: N818 - a refusal of the model's answer
    """The model's answer is not a persona turn, even after one corrective re-ask."""


@dataclass
class PersonaContext:
    """What the persona sees: the scenario, its phase and the redacted dialog."""

    instruction: str
    transcript: list[dict] = field(default_factory=list)
    latest: list[dict] = field(default_factory=list)


class Persona(Protocol):
    async def turn(self, context: PersonaContext) -> PersonaTurn: ...


#: What a product-only customer never says. Matched on whole-word stems.
FORBIDDEN_TERMS = (
    r"модул\w*",
    r"module\w*",
    r"пакет\w*",
    r"package\w*",
    r"библиотек\w*",
    r"librar\w*",
    r"верси\w*",
    r"version\w*",
    r"платформ\w*",
    r"platform\w*",
    r"kit",
    r"каталог\w*",
    r"catalog\w*",
    r"api",
    r"ключ(?:а|ом|и|ей|у|ам|ами|ах)?",
    r"key",
    r"tg-channels",
    r"pip",
    r"docker\w*",
    r"github",
    r"репозитор\w*",
    r"repositor\w*",
    r"сервер\w*",
    r"server\w*",
    r"деплой\w*",
    r"deploy\w*",
    r"python",
    r"код(?:а|ом|у|е|ы|ов|ами|ах)?",
)
_FORBIDDEN = re.compile(r"(?<![\w-])(?:" + "|".join(FORBIDDEN_TERMS) + r")(?![\w-])", re.I)
_CYRILLIC = re.compile(r"[а-яё]", re.I)
_LATIN = re.compile(r"[a-z]", re.I)
_CREDENTIAL = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}|cps_[a-z2-7]{12}_|[A-Z0-9_-]{20,}")


def deviation(turn: PersonaTurn, scenario: Scenario) -> str | None:
    """Why this turn leaves the customer's scenario, or None when it stays in it."""
    if turn.decision is PersonaDecision.REPLY:
        text = (turn.text or "").strip()
        if not text:
            return "empty_reply"
        if _FORBIDDEN.search(text):
            return "implementation_instruction"
        if _CREDENTIAL.search(text):
            return "credential_shaped_text"
        channels = {f"@{name}" for name in scenario.public_channels}
        mentioned = {match.lower() for match in re.findall(r"@[A-Za-z0-9_]+", text)}
        if mentioned - channels:
            return "unknown_channel"
        prose = re.sub(r"@[A-Za-z0-9_]+", " ", text)
        cyrillic = len(_CYRILLIC.findall(prose))
        if cyrillic == 0 or cyrillic < 2 * len(_LATIN.findall(prose)):
            return "not_russian"
    if turn.decision is PersonaDecision.PRESS and not (turn.button or "").strip():
        return "press_without_button"
    return None


def persona_prompt(scenario: Scenario) -> str:
    channels = ", ".join(f"@{name}" for name in scenario.public_channels)
    return f"""\
Ты — заказчик, который пишет боту-разработчику в Telegram. Ты не программист и \
ничего не знаешь о том, как делают ботов.

Чего ты хочешь: нового Telegram-бота для себя, который присылает тебе новые посты из \
публичных каналов {channels}. Ещё хорошо бы видеть список своих каналов и получать \
подборку последних постов по команде. Язык бота — русский.

Правила:
- Пиши только по-русски, коротко и по делу, как обычный заказчик.
- Никогда не говори о том, как это сделать: ни слова о модулях, пакетах, библиотеках, \
версиях, платформе, ключах, API, коде, серверах, репозиториях или развёртывании.
- Не упоминай других каналов, кроме {channels}.
- Если бот спрашивает язык бота, выбирай русский.
- Никогда не пиши токен, пароль или код: их отправляют без тебя.
- Если бот предлагает разбить работу на этапы, соглашайся.
- Если бот показывает описание заказа и оно совпадает с твоим желанием, подтверди его.
- Если бот ещё работает или ничего не спросил — decision="wait".
- Если бот говорит, что сделать это нельзя — decision="impossible".
- Кнопку нажимай только видимую, по её точной подписи (decision="press", button=подпись).

Ответь одним JSON-объектом без пояснений, с полями: decision ("reply"|"press"|"wait"|\
"impossible"), text (строка или null), button (строка или null).
"""


def parse_turn(content: Any) -> PersonaTurn:
    text = content if isinstance(content, str) else json.dumps(content)
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    try:
        return PersonaTurn.model_validate(json.loads(text))
    except (ValueError, ValidationError):
        raise PersonaInvalid("the persona answer is not one valid JSON turn") from None


class ModelPersona:
    """The persona answered by a chat model (anything with LangChain's `ainvoke`)."""

    def __init__(self, model: Any, scenario: Scenario) -> None:
        self._model = model
        self._system = persona_prompt(scenario)

    async def turn(self, context: PersonaContext) -> PersonaTurn:
        payload = json.dumps(
            {
                "instruction": context.instruction,
                "dialog": context.transcript,
                "new_bot_messages": context.latest,
            },
            ensure_ascii=False,
        )
        messages = [SystemMessage(self._system), HumanMessage(payload)]
        answer = await self._model.ainvoke(messages)
        try:
            return parse_turn(answer.content)
        except PersonaInvalid:
            messages += [
                answer,
                HumanMessage("Ответ не прошёл проверку: верни только один JSON-объект по схеме."),
            ]
            return parse_turn((await self._model.ainvoke(messages)).content)
