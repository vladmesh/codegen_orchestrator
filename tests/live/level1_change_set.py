"""The change sets the level-1 story hands the scripted developer.

The level-1 story is a Telegram-bot product: two engineering tasks, each
carrying one change set in its description, applied by the merged scripted
runner (``packages/worker-wrapper/src/worker_wrapper/runners/noop.py``). Between
them they add the three things the suite then asserts against the *deployed*
product:

* a backend endpoint — ``GET /level1/marker`` on the product's own API;
* a product-scoped setting — one property in ``services/backend/manifest.yaml``,
  which ``make setup`` turns into an entry of the generated
  ``SETTINGS_SCHEMAS`` registry the endpoint reads back;
* a Telegram command handler — ``/level1`` in ``services/tg_bot``, published to
  Telegram with ``setMyCommands`` when the deployed bot starts.

Everything is keyed on one per-run marker, so nothing an earlier run left behind
— a committed file, a cached image, a command menu Telegram still remembers —
can answer for this run.

Written against the pin, not against a remembered tree
------------------------------------------------------

Every ``replace`` here is built from the vendored render of the pinned kit
(``scripts.template_pin``): the file is read from the fixture and edited by
anchored substitution, and a missing anchor raises. A kit whose tree moved under
the pin therefore fails here, in a unit test, instead of failing ``make setup``
on the stand — and the change set can never be a stale hand-copy of a file the
kit has since changed.

``build_level1_change_sets`` is the one entry point. It refuses to build for a
template other than the pinned one, because the fixture it edits is a render of
that pin and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.template_pin import TEMPLATE_PIN

#: The fence the scripted runner recognises, and the sentinel it demands first.
CHANGE_SET_FENCE = "codegen-change-set"
CHANGE_SET_SENTINEL = "codegen-change-set v1"

#: The product-scoped setting the backend manifest registers.
LEVEL1_SETTING_KEY = "level1_marker"
#: The backend endpoint the change set adds, on the product's own API.
LEVEL1_ENDPOINT_PATH = "/level1/marker"
#: The Telegram command the bot change set adds. Telegram allows only
#: ``[a-z0-9_]{1,32}`` in a command name, so the marker travels in the
#: description beside it rather than in the name.
LEVEL1_COMMAND = "level1"

BACKEND_MANIFEST = "services/backend/manifest.yaml"
#: Where the endpoint module goes. The kit's own gate
#: (``framework.enforce_spec_compliance``) forbids an ``APIRouter()`` call
#: outside ``app/api/routers/`` — ``in_routers = "routers" in file_path.parts``
#: — so the product's hand-written route lives there rather than beside
#: ``v1/health.py``. The generator writes its own routers to
#: ``src/generated/routers/``, so this package stays the product's own.
BACKEND_ENDPOINT_MODULE = "services/backend/src/app/api/routers/level1.py"
BACKEND_ROUTER = "services/backend/src/app/api/router.py"
BOT_MENU_MODULE = "services/tg_bot/src/menu.py"
BOT_MAIN = "services/tg_bot/src/main.py"


class ChangeSetAnchorMissing(RuntimeError):
    """A file of the pinned kit no longer holds the text this edit is anchored on."""


@dataclass(frozen=True)
class Operation:
    """One ``@@ <op> <path>`` directive and the content that follows it."""

    op: str
    path: str
    content: str


def level1_command_description(marker: str) -> str:
    """What the deployed bot publishes to Telegram beside ``/level1``."""
    return f"level-1 product marker {marker}"


def _fixture_text(relative: str) -> str:
    path: Path = TEMPLATE_PIN.fixture_path() / relative
    return path.read_text(encoding="utf-8")


def _substitute(text: str, anchor: str, replacement: str, *, where: str) -> str:
    """Replace one anchor exactly once, or say which file stopped holding it."""
    occurrences = text.count(anchor)
    if occurrences != 1:
        raise ChangeSetAnchorMissing(
            f"{where}: the pinned kit holds {occurrences} occurrences of {anchor!r}, expected 1"
        )
    return text.replace(anchor, replacement)


def _backend_manifest(marker: str) -> str:
    """Register one product-scoped setting in the backend's own manifest.

    The manifest is the only place a product declares a settings key;
    ``make setup`` runs ``framework.generate``, which turns the declaration into
    an entry of ``services/backend/src/generated/settings_schemas.py``. Keeping
    the surrounding document byte-for-byte — comments included — is why this is
    an anchored substitution rather than a YAML round-trip.
    """
    declaration = (
        "  properties:\n"
        f"    {LEVEL1_SETTING_KEY}:\n"
        "      type: string\n"
        "      minLength: 1\n"
        f"      default: {marker}\n"
    )
    return _substitute(
        _fixture_text(BACKEND_MANIFEST),
        "settings_schema:\n"
        "  $schema: https://json-schema.org/draft/2020-12/schema\n"
        "  type: object\n"
        "  properties: {}\n",
        "settings_schema:\n"
        "  $schema: https://json-schema.org/draft/2020-12/schema\n"
        "  type: object\n" + declaration,
        where=BACKEND_MANIFEST,
    )


def _backend_endpoint(marker: str) -> str:
    """The product's own endpoint, reporting what its manifest registered."""
    return f'''"""The level-1 marker endpoint this product's scripted change set added.

It answers with the marker the change set was written for and with the settings
registry the framework generated from ``services/backend/manifest.yaml``, so the
*running* deployment can be asked whether the declaration reached it. A manifest
edit that never went through ``make setup`` leaves the registry empty here.
"""

from typing import Any

from fastapi import APIRouter

from services.backend.src.generated.settings_schemas import SETTINGS_SCHEMAS

LEVEL1_MARKER = "{marker}"
LEVEL1_SETTING_KEY = "{LEVEL1_SETTING_KEY}"

router = APIRouter()


@router.get("{LEVEL1_ENDPOINT_PATH}", summary="Level-1 product marker")
async def level1_marker() -> dict[str, Any]:
    """Report this product's marker and every settings key its manifests declare."""
    return {{
        "marker": LEVEL1_MARKER,
        "setting_key": LEVEL1_SETTING_KEY,
        "declared_settings": dict(SETTINGS_SCHEMAS),
    }}


__all__ = ["router"]
'''


def _backend_router() -> str:
    """Mount the new endpoint beside the kit's own health router."""
    text = _substitute(
        _fixture_text(BACKEND_ROUTER),
        "from .v1.health import router as health_router\n",
        "from .routers.level1 import router as level1_router\n"
        "from .v1.health import router as health_router\n",
        where=BACKEND_ROUTER,
    )
    return _substitute(
        text,
        'api_router.include_router(health_router, tags=["health"])\n',
        'api_router.include_router(health_router, tags=["health"])\n'
        'api_router.include_router(level1_router, tags=["level1"])\n',
        where=BACKEND_ROUTER,
    )


def _bot_menu(marker: str) -> str:
    """The new command handler, and the startup step that publishes it."""
    return f'''"""The level-1 Telegram command this product's scripted change set added.

``register_command_menu`` publishes the bot's command list with
``setMyCommands`` once the application has started, so Telegram itself can be
asked what the *running* deployment registered. The marker travels in the
description because Telegram accepts only ``[a-z0-9_]`` in a command name.
"""

from __future__ import annotations

import structlog
from telegram import BotCommand, Update
from telegram.ext import Application, ContextTypes

LOGGER = structlog.stdlib.get_logger()

LEVEL1_COMMAND = "{LEVEL1_COMMAND}"
LEVEL1_MARKER = "{marker}"
LEVEL1_COMMAND_DESCRIPTION = "{level1_command_description(marker)}"
LEVEL1_REPLY = f"level-1 marker: {{LEVEL1_MARKER}}"


async def handle_level1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to /{LEVEL1_COMMAND} with this product's marker."""
    if update.message is None:
        return
    await update.message.reply_text(LEVEL1_REPLY)
    LOGGER.info("level1_command_handled")


async def register_command_menu(application: Application) -> None:
    """Publish the running bot's command list to Telegram."""
    await application.bot.set_my_commands(
        [
            BotCommand("start", "start the bot"),
            BotCommand("command", "publish a command event"),
            BotCommand(LEVEL1_COMMAND, LEVEL1_COMMAND_DESCRIPTION),
        ]
    )
    LOGGER.info("command_menu_published", command=LEVEL1_COMMAND)
'''


def _bot_main() -> str:
    """Wire the new handler and the menu publication into the bot's entry point.

    ``post_init`` keeps its exact signature and its exact body: the kit's own
    unit test calls it directly with a ``MagicMock`` application, and awaiting
    ``application.bot.set_my_commands`` on a mock would fail that test. The new
    ``startup`` composes the two steps instead, and it is what the builder wires.
    """
    access_import = (
        "from services.tg_bot.src.access import TELEGRAM_CHANNEL, is_active, telegram_external_id\n"
    )
    menu_import = (
        "from services.tg_bot.src.menu import "
        "LEVEL1_COMMAND, handle_level1, register_command_menu\n"
    )
    text = _substitute(
        _fixture_text(BOT_MAIN),
        access_import,
        access_import + menu_import,
        where=BOT_MAIN,
    )
    text = _substitute(
        text,
        "async def post_shutdown(application: Application) -> None:\n",
        "async def startup(application: Application) -> None:\n"
        '    """Run every post-init step of this bot, in order."""\n'
        "    await post_init(application)\n"
        "    await register_command_menu(application)\n"
        "\n"
        "\n"
        "async def post_shutdown(application: Application) -> None:\n",
        where=BOT_MAIN,
    )
    text = _substitute(
        text,
        "        .post_init(post_init)\n",
        "        .post_init(startup)\n",
        where=BOT_MAIN,
    )
    return _substitute(
        text,
        '    application.add_handler(CommandHandler("command", handle_command))\n',
        '    application.add_handler(CommandHandler("command", handle_command))\n'
        "    application.add_handler(CommandHandler(LEVEL1_COMMAND, handle_level1))\n",
        where=BOT_MAIN,
    )


def backend_operations(marker: str) -> list[Operation]:
    """The first task: the endpoint and the product-scoped setting behind it."""
    return [
        Operation("replace", BACKEND_MANIFEST, _backend_manifest(marker)),
        Operation("create", BACKEND_ENDPOINT_MODULE, _backend_endpoint(marker)),
        Operation("replace", BACKEND_ROUTER, _backend_router()),
    ]


def bot_operations(marker: str) -> list[Operation]:
    """The second task: the Telegram command handler and its published menu."""
    return [
        Operation("create", BOT_MENU_MODULE, _bot_menu(marker)),
        Operation("replace", BOT_MAIN, _bot_main()),
    ]


def render_change_set(operations: list[Operation]) -> str:
    """The single fenced block the scripted runner reads out of ``TASK.md``."""
    lines = [f"```{CHANGE_SET_FENCE}", CHANGE_SET_SENTINEL]
    for operation in operations:
        lines.append(f"@@ {operation.op} {operation.path}")
        lines.extend(operation.content.splitlines())
    lines.append("```")
    return "\n".join(lines)


@dataclass(frozen=True)
class Level1ChangeSets:
    """Both engineering tasks of the level-1 story, and what they add."""

    marker: str
    backend: list[Operation]
    bot: list[Operation]

    @property
    def paths(self) -> list[str]:
        """Every workspace path the story's change sets touch."""
        return [operation.path for operation in (*self.backend, *self.bot)]

    def backend_task_description(self) -> str:
        return _task_description(
            "Add the level-1 marker endpoint and register its product setting.",
            self.backend,
        )

    def bot_task_description(self) -> str:
        return _task_description(
            f"Add the /{LEVEL1_COMMAND} Telegram command and publish the bot's command menu.",
            self.bot,
        )


def _task_description(headline: str, operations: list[Operation]) -> str:
    """One task description: the sentence a human reads, then the change set.

    The change set travels in the task description because that is the channel
    the manager already writes to ``/workspace/TASK.md``; nothing else is added
    to carry it. Exactly one fenced block per description — the runner refuses a
    document holding more than one.
    """
    return f"{headline}\n\n{render_change_set(operations)}\n"


def build_level1_change_sets(marker: str, template: tuple[str, str]) -> Level1ChangeSets:
    """Build both change sets, refusing a template the fixture is not a render of.

    Every ``replace`` is an edit of the vendored render of the pinned kit. A run
    pointed at some other template would be handed edits of a tree it does not
    have, and ``replace`` would overwrite live files with the pinned kit's
    content. That is refused here, at the start of the run, rather than
    discovered as a red ``make setup`` an hour later.
    """
    pinned = (TEMPLATE_PIN.source, TEMPLATE_PIN.ref)
    if template != pinned:
        raise RuntimeError(
            "the level-1 change set is written against the pinned kit render in "
            f"{TEMPLATE_PIN.fixture_relpath}, so it cannot be applied to template "
            f"{template[0]}@{template[1]}; the pin is {pinned[0]}@{pinned[1]}"
        )
    return Level1ChangeSets(
        marker=marker,
        backend=backend_operations(marker),
        bot=bot_operations(marker),
    )
