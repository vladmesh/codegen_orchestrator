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

The *extension* story — the second story of the same project — has its own
change set at the end of this module, keyed on its own marker, whose edits are
anchored on the first story's text rather than repeating it. Its one task adds a
second endpoint (``GET /level1/extension``) and a second product-scoped setting,
so the deployment can be asked whether the second story reached it and whether
the first story's work is still there beside it.
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
#: The product-scoped setting the *extension* story's change set adds beside it.
#: A second key, not a second value of the first: the extension story has to be
#: able to fail on its own, and a value written over the first story's key would
#: be indistinguishable from the first story's seed.
LEVEL1_EXTENSION_SETTING_KEY = "level1_extension_marker"
#: The backend endpoint the change set adds, on the product's own API.
LEVEL1_ENDPOINT_PATH = "/level1/marker"
#: The backend endpoint the extension story's change set adds.
LEVEL1_EXTENSION_ENDPOINT_PATH = "/level1/extension"
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
#: The extension story's own module, beside it and never over it: a `create`
#: cannot silently lose the first story's endpoint.
BACKEND_EXTENSION_MODULE = "services/backend/src/app/api/routers/level1_extension.py"
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


def backend_acceptance_criteria(marker: str) -> str:
    """What QA checks the backend task by, on the running deployment.

    The brief's `level1_setting` requirement, stated as observations a QA
    executor can make: the endpoint the change set adds answers, it answers with
    *this run's* marker, and the settings key the confirmed brief seeded is
    registered on the deployment the answer came from. Every line is something
    the suite's own deployed-product probes already read, so this is the check
    QA would make and not a restatement of the diff.

    The marker is in it on purpose. It is minted per run, so a `TASK.md` left
    behind by an earlier run — in a cached image, a stale workspace, a document
    nobody rewrote — cannot satisfy a verbatim check against these criteria.
    """
    return (
        f"- GET {LEVEL1_ENDPOINT_PATH} on the deployed backend answers HTTP 200.\n"
        f'- The JSON it answers with carries "marker" exactly equal to "{marker}".\n'
        f'- That JSON carries "{LEVEL1_SETTING_KEY}" among the keys of "declared_settings", '
        "so the product setting the confirmed brief seeded is registered on the running "
        "deployment."
    )


def bot_acceptance_criteria(marker: str) -> str:
    """What QA checks the bot task by, on the running deployment.

    The brief's `level1_command` requirement: the command answers with this
    run's marker, and the running bot publishes that command to Telegram. Same
    per-run marker discipline as `backend_acceptance_criteria`.
    """
    return (
        f"- The deployed bot answers the command /{LEVEL1_COMMAND} with exactly "
        f'"level-1 marker: {marker}".\n'
        f"- The command menu the running bot publishes to Telegram lists /{LEVEL1_COMMAND} "
        f'with the description "{level1_command_description(marker)}".'
    )


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


def _setting_declaration(key: str, default: str) -> str:
    """One product-scoped string property of the backend manifest, as YAML.

    Shared by the first story's declaration and the extension story's, so the
    two are the same shape of document by construction and the extension's
    anchored substitution has an exact text to anchor on.
    """
    return f"    {key}:\n      type: string\n      minLength: 1\n      default: {default}\n"


def _backend_manifest(marker: str) -> str:
    """Register one product-scoped setting in the backend's own manifest.

    The manifest is the only place a product declares a settings key;
    ``make setup`` runs ``framework.generate``, which turns the declaration into
    an entry of ``services/backend/src/generated/settings_schemas.py``. Keeping
    the surrounding document byte-for-byte — comments included — is why this is
    an anchored substitution rather than a YAML round-trip.
    """
    declaration = "  properties:\n" + _setting_declaration(LEVEL1_SETTING_KEY, marker)
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

    def backend_acceptance_criteria(self) -> str:
        """What the first task's `TASK.md` has to quote, word for word."""
        return backend_acceptance_criteria(self.marker)

    def bot_acceptance_criteria(self) -> str:
        """What the second task's `TASK.md` has to quote, word for word."""
        return bot_acceptance_criteria(self.marker)


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


# ── The extension story: the second story of the same project ────────────
#
# The second story of a project is where five of the seven regressions
# sprint:1445 found by hand lived, so the level-1 lifecycle runs one. It needs a
# change set of its own for the same reason the first one does: the deployed
# product has to be able to show that *this* story reached it, and a story that
# changed nothing cannot. The marker is minted per run and per story, so neither
# the first story's deployment nor an earlier run can answer for it.
#
# Every edit below is anchored on the *first story's* text — which is itself an
# anchored edit of the pinned kit render — so a kit that moved under the pin
# still fails offline, one substitution earlier, rather than on the stand.


def extension_acceptance_criteria(marker: str, extension_marker: str) -> str:
    """What QA checks the extension task by, on the running deployment.

    The extension brief's one requirement, stated as observations: the second
    endpoint answers, it answers with this story's marker, the *first* story's
    marker is still there beside it — a deployment that lost the first story's
    work is a failure of this story, not a success — and the setting the
    corrected brief seeded is registered on the deployment that answered.

    Both markers are in it on purpose, and the same per-run discipline applies
    as for the first story's criteria: a `TASK.md` a previous attempt, a cached
    image or the first story left in the reused workspace cannot satisfy a
    verbatim check against these.
    """
    return (
        f"- GET {LEVEL1_EXTENSION_ENDPOINT_PATH} on the deployed backend answers HTTP 200.\n"
        f'- The JSON it answers with carries "marker" exactly equal to "{extension_marker}".\n'
        f'- That JSON carries "base_marker" exactly equal to "{marker}", so the deployment '
        "still carries the first story's work.\n"
        f'- That JSON carries "{LEVEL1_EXTENSION_SETTING_KEY}" among the keys of '
        '"declared_settings", so the product setting the corrected brief seeded is registered '
        "on the running deployment."
    )


def _backend_manifest_with_extension(marker: str, extension_marker: str) -> str:
    """Declare the extension story's setting beside the first story's, not over it."""
    first = _setting_declaration(LEVEL1_SETTING_KEY, marker)
    return _substitute(
        _backend_manifest(marker),
        first,
        first + _setting_declaration(LEVEL1_EXTENSION_SETTING_KEY, extension_marker),
        where=BACKEND_MANIFEST,
    )


def _backend_extension_endpoint(marker: str, extension_marker: str) -> str:
    """The extension story's own endpoint, reporting both stories' markers.

    ``base_marker`` is the first story's, read from this module rather than from
    the first story's module on purpose: what the deployed product answers has
    to come from the tree the *second* story's branch was built from, and that
    tree carries the first story's work only if the branch was cut from a
    default branch containing its merge.
    """
    return f'''"""The level-1 extension endpoint the second story's change set added.

It answers with the marker minted for the extension story, with the first
story's marker beside it, and with the settings registry the framework generated
from ``services/backend/manifest.yaml`` — so the running deployment can be asked
whether both stories reached it.
"""

from typing import Any

from fastapi import APIRouter

from services.backend.src.generated.settings_schemas import SETTINGS_SCHEMAS

LEVEL1_EXTENSION_MARKER = "{extension_marker}"
LEVEL1_BASE_MARKER = "{marker}"
LEVEL1_EXTENSION_SETTING_KEY = "{LEVEL1_EXTENSION_SETTING_KEY}"

router = APIRouter()


@router.get("{LEVEL1_EXTENSION_ENDPOINT_PATH}", summary="Level-1 extension marker")
async def level1_extension_marker() -> dict[str, Any]:
    """Report the extension marker, the first story's marker and every declared setting."""
    return {{
        "marker": LEVEL1_EXTENSION_MARKER,
        "base_marker": LEVEL1_BASE_MARKER,
        "setting_key": LEVEL1_EXTENSION_SETTING_KEY,
        "declared_settings": dict(SETTINGS_SCHEMAS),
    }}


__all__ = ["router"]
'''


def _backend_router_with_extension() -> str:
    """Mount the extension endpoint beside the first story's, keeping both."""
    text = _substitute(
        _backend_router(),
        "from .routers.level1 import router as level1_router\n",
        "from .routers.level1 import router as level1_router\n"
        "from .routers.level1_extension import router as level1_extension_router\n",
        where=BACKEND_ROUTER,
    )
    return _substitute(
        text,
        'api_router.include_router(level1_router, tags=["level1"])\n',
        'api_router.include_router(level1_router, tags=["level1"])\n'
        'api_router.include_router(level1_extension_router, tags=["level1"])\n',
        where=BACKEND_ROUTER,
    )


def extension_operations(marker: str, extension_marker: str) -> list[Operation]:
    """The extension story's one task: a second endpoint and the setting behind it."""
    return [
        Operation(
            "replace", BACKEND_MANIFEST, _backend_manifest_with_extension(marker, extension_marker)
        ),
        Operation(
            "create",
            BACKEND_EXTENSION_MODULE,
            _backend_extension_endpoint(marker, extension_marker),
        ),
        Operation("replace", BACKEND_ROUTER, _backend_router_with_extension()),
    ]


@dataclass(frozen=True)
class Level1ExtensionChangeSet:
    """The single engineering task of the extension story, and what it adds."""

    marker: str
    extension_marker: str
    operations: list[Operation]

    @property
    def paths(self) -> list[str]:
        """Every workspace path the extension change set touches."""
        return [operation.path for operation in self.operations]

    def task_description(self) -> str:
        return _task_description(
            "Add the level-1 extension endpoint and register its product setting.",
            self.operations,
        )

    def acceptance_criteria(self) -> str:
        """What the extension task's `TASK.md` has to quote, word for word."""
        return extension_acceptance_criteria(self.marker, self.extension_marker)


def build_level1_extension_change_set(
    marker: str, extension_marker: str, template: tuple[str, str]
) -> Level1ExtensionChangeSet:
    """Build the extension change set, refusing a template the fixture is not a render of.

    The same refusal `build_level1_change_sets` makes, for the same reason: every
    ``replace`` here is an edit of an edit of the pinned kit render, so a run
    pointed at another template would be handed the pinned kit's content to
    overwrite live files with.
    """
    pinned = (TEMPLATE_PIN.source, TEMPLATE_PIN.ref)
    if template != pinned:
        raise RuntimeError(
            "the level-1 extension change set is written against the pinned kit render in "
            f"{TEMPLATE_PIN.fixture_relpath}, so it cannot be applied to template "
            f"{template[0]}@{template[1]}; the pin is {pinned[0]}@{pinned[1]}"
        )
    return Level1ExtensionChangeSet(
        marker=marker,
        extension_marker=extension_marker,
        operations=extension_operations(marker, extension_marker),
    )
