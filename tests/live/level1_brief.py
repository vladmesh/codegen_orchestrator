"""The confirmed Product Brief the level-1 bot product is built against.

The level-1 run is the free deterministic one: no model is asked anything, at
any stage. That holds for the brief too. The document below is the product
contract written down here, once, and driven through the *released* PO tools —
`present_product_brief`, `confirm_product_brief`, `create_story` — exactly as
`tests/live/brief_pipeline.py::_po_create_confirmed_story` drives them for the
paid variants. What a model would have composed is a constant here; nothing
else about the boundary changes.

Two things the rest of the suite reads out of this module:

* the brief itself — Russian, one usage example per user-facing requirement,
  its limitations, and the one product-scoped ``initial_settings`` entry whose
  key is the one the level-1 change set declares in the backend manifest. That
  key is what makes the deploy's settings seed land in a product that can
  accept it, and what makes the value readable back from the deployment;
* what a *bot* product's completion message has to be. A bot owner is told how
  to reach their bot and how to use it, in the language they confirmed the
  brief in, and is never given a server address
  (``services/api/src/routers/stories.py::_telegram_bot_usage_instructions``).
  `bot_completion_message_mismatches` states that as a predicate so the live
  wait and the offline regressions judge one thing.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from level1_change_set import LEVEL1_COMMAND, LEVEL1_SETTING_KEY

#: The language the level-1 user confirms their brief in. Deliberately not the
#: harness's own language: the completion message is composed in the *brief's*
#: language, and a brief in English would make that indistinguishable from a
#: default.
LEVEL1_BRIEF_LANGUAGE = "ru"

#: The two must-requirements, one per engineering task of the level-1 story.
LEVEL1_COMMAND_REQUIREMENT = "level1_command"
LEVEL1_SETTING_REQUIREMENT = "level1_setting"

#: The only link a bot product's completion message may carry.
BOT_LINK_PREFIX = "https://t.me/"

_URL_RE = re.compile(r"https?://[^\s,;)]+")


@dataclass(frozen=True)
class Level1Brief:
    """One run's level-1 product contract, keyed on that run's marker.

    Everything the user is shown is Russian, because that is what `language`
    says; everything the platform keys on — the requirement ids, the settings
    key — is the machine-readable side and stays as the product declares it.
    """

    marker: str
    title: str
    summary: str
    must_requirements: tuple[dict[str, str], ...]
    usage_examples: tuple[dict[str, str], ...]
    limitations: tuple[str, ...]
    settings_key: str
    settings_value: str
    settings_description: str
    story_title: str
    story_description: str
    language: str = LEVEL1_BRIEF_LANGUAGE

    @property
    def requirement_ids(self) -> list[str]:
        """The ids every disposition and every admission is counted over."""
        return [requirement["id"] for requirement in self.must_requirements]

    def present_arguments(self, project_id: str) -> dict:
        """Exactly what `present_product_brief` is called with, and nothing more."""
        return {
            "project_id": project_id,
            "title": self.title,
            "summary": self.summary,
            "must_requirements": [dict(one) for one in self.must_requirements],
            "language": self.language,
            "usage_examples": [dict(one) for one in self.usage_examples],
            "limitations": list(self.limitations),
            "initial_settings": [
                {
                    "key": self.settings_key,
                    "scope": "product",
                    "value": self.settings_value,
                    "description": self.settings_description,
                }
            ],
        }


def level1_settings_value(marker: str) -> str:
    """The value the user confirms for the product setting.

    Deliberately *not* the marker itself. The change set declares the marker as
    the key's manifest `default`, so a readback equal to the marker would be
    satisfied by a product that was never seeded. This value exists nowhere but
    in the confirmed brief, so reading it back off the deployment proves the
    seed wrote it.
    """
    return f"{marker}-confirmed"


def build_level1_brief(marker: str) -> Level1Brief:
    """The level-1 product contract for one run, in the user's own language."""
    return Level1Brief(
        marker=marker,
        title="Бот с маркером уровня 1",
        summary=(
            "Телеграм-бот, который по команде показывает маркер продукта, "
            "а сам маркер задаётся одной настройкой продукта."
        ),
        must_requirements=(
            {
                "id": LEVEL1_COMMAND_REQUIREMENT,
                "text": f"Бот отвечает на команду /{LEVEL1_COMMAND} маркером этого продукта.",
                "user_wording": (
                    f"Хочу команду /{LEVEL1_COMMAND}, чтобы бот присылал маркер продукта."
                ),
            },
            {
                "id": LEVEL1_SETTING_REQUIREMENT,
                "text": (
                    f"Маркер продукта хранится в настройке {LEVEL1_SETTING_KEY} "
                    "и действует в развёрнутом продукте."
                ),
                "user_wording": (
                    "Маркер должен лежать в настройках продукта, чтобы его можно было поменять."
                ),
            },
        ),
        usage_examples=(
            {
                "requirement_id": LEVEL1_COMMAND_REQUIREMENT,
                "user_sends": f"команду /{LEVEL1_COMMAND} боту",
                "product_answers": "сообщение с маркером этого продукта",
            },
            {
                "requirement_id": LEVEL1_SETTING_REQUIREMENT,
                "user_sends": "вопрос, какой маркер сейчас настроен",
                "product_answers": "значение, подтверждённое в настройке продукта",
            },
        ),
        limitations=(
            "Маркер один на весь продукт: отдельных маркеров для каждого пользователя нет.",
        ),
        settings_key=LEVEL1_SETTING_KEY,
        settings_value=level1_settings_value(marker),
        settings_description=(
            f"Маркер продукта, который бот показывает по команде /{LEVEL1_COMMAND}."
        ),
        story_title="Маркер уровня 1: команда бота и настройка продукта",
        story_description=(
            f"Бот отвечает на /{LEVEL1_COMMAND} маркером продукта, "
            f"а маркер берётся из настройки продукта {LEVEL1_SETTING_KEY}."
        ),
    )


def bot_completion_message_mismatches(
    text: str,
    *,
    bot_username: str,
    usage_examples: list[dict],
    language: str,
) -> list[str]:
    """Why this is not the completion message a *bot* product sends, if it is not.

    Three claims, each about the message's behaviour for its reader rather than
    about its wording:

    * it names the bot the owner reaches, so the owner knows where to go;
    * it carries every usage example of the confirmed brief, in the language the
      brief was confirmed in — the examples are that language, and the message
      names the code as well;
    * it gives no server, API or backend address. Stated as "every link in it is
      the bot's own t.me link", so a *different* backend address is refused too,
      not only the one this run happens to have deployed.

    An empty list means the message is a bot product's. Every entry is one
    reason, phrased so a timed-out wait can print it as it stands.
    """
    reasons: list[str] = []
    handle = f"@{bot_username}"
    if handle not in text:
        reasons.append(f"it does not name the bot {handle}")
    if f"({language})" not in text:
        reasons.append(f"it does not name the brief's language ({language})")
    for example in usage_examples:
        for field in ("user_sends", "product_answers"):
            fragment = example[field]
            if fragment not in text:
                reasons.append(
                    f"it does not carry the confirmed usage example "
                    f"{example['requirement_id']}.{field}: {fragment!r}"
                )
    addresses = sorted(
        {url for url in _URL_RE.findall(text) if not url.startswith(BOT_LINK_PREFIX)}
    )
    if addresses:
        reasons.append(f"it gives the user an address that is not the bot's: {addresses}")
    return reasons
