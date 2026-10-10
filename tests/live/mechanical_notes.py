"""First-story notes customization for the mechanical stand, before any install."""

from dataclasses import replace

from level1_change_set import (
    BACKEND_ROUTER,
    BOT_COMMANDS,
    LEVEL1_COMMAND,
    Operation,
    _backend_router,
    _bot_commands,
    _fixture_text,
    _substitute,
    backend_operations,
    bot_operations,
    render_change_set,
)

NOTES_ROUTER = "services/backend/src/app/api/routers/notes.py"
NOTES_HANDLERS = "services/tg_bot/src/handlers/notes.py"

# Stored in the product Redis, whose volume survives ordinary image deployments.
# No reminder implementation, timer, binding or component enters the first story.
# The locked redis 7.x types each command `Awaitable[T] | T`; the casts keep the
# install's product mypy green (stand-e2e run 37567975527).
BACKEND_NOTES = '''"""Product-owned notes, scoped to the core-verified caller."""
import os
from collections.abc import Awaitable
from typing import Annotated, cast
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from codegen_kit import caller_identity

router = APIRouter()
Caller = Annotated[str, Depends(caller_identity)]

class Note(BaseModel):
    text: str = Field(min_length=1, max_length=1000)

@router.post("/notes")
async def save_note(note: Note, owner: Caller) -> Note:
    async with Redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as redis:
        await cast(Awaitable[int], redis.rpush("notes:" + owner, note.text))
    return note

@router.get("/notes")
async def list_notes(owner: Caller) -> list[str]:
    async with Redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as redis:
        notes = await cast(Awaitable[list[str]], redis.lrange("notes:" + owner, 0, -1))
    return notes
'''

BOT_NOTES = '''"""Product-owned save/list commands."""
from telegram import Update
from telegram.ext import ContextTypes

async def handle_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.tg_bot.src.main import BackendClient
    if update.message is None or update.effective_user is None:
        return
    async with BackendClient() as client:
        response = await client.request_as_telegram_user(
            "post", "/notes", update.effective_user.id, json={"text": " ".join(context.args or [])})
        response.raise_for_status()
        await update.message.reply_text("Saved: " + response.json()["text"])

async def handle_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.tg_bot.src.main import BackendClient
    if update.message is None or update.effective_user is None:
        return
    async with BackendClient() as client:
        response = await client.request_as_telegram_user("get", "/notes", update.effective_user.id)
        response.raise_for_status()
        await update.message.reply_text("\\n".join(response.json()) or "No notes.")
'''


def _notes_router():
    """Mount the notes router inside the router module's own import and include blocks.

    Appending the import after `__all__` is an E402 the product's `pre-commit`
    (`make format` → `ruff check --fix`) refuses, so the commit step fails.
    """
    text = _substitute(
        _backend_router(),
        "from .routers.level1 import router as level1_router\n",
        "from .routers.level1 import router as level1_router\n"
        "from .routers.notes import router as notes_router\n",
        where="notes router import",
    )
    return _substitute(
        text,
        'api_router.include_router(level1_router, tags=["level1"])\n',
        'api_router.include_router(level1_router, tags=["level1"])\n'
        'api_router.include_router(notes_router, tags=["notes"])\n',
        where="notes router mount",
    )


def notes_operations(marker):
    backend = [one for one in backend_operations(marker) if one.path != BACKEND_ROUTER]
    backend += [
        Operation("create", NOTES_ROUTER, BACKEND_NOTES),
        Operation("replace", BACKEND_ROUTER, _notes_router()),
    ]
    backend.append(
        Operation(
            "replace",
            "services/backend/pyproject.toml",
            # Redis is already locked and installed through faststream[redis].
            # Declare its product-owned direct use without changing frozen deps.
            _substitute(
                _fixture_text("services/backend/pyproject.toml"),
                'DEP003 = ["starlette", "structlog"]',
                'DEP003 = ["starlette", "structlog", "redis"]',
                where="notes backend dependency declaration",
            ),
        )
    )
    # The kit registers bot commands only through its registry: the notes commands are
    # declared beside the level-1 command, and `make setup` regenerates the registry.
    level1 = (
        "COMMANDS: tuple[ProductCommand, ...] = "
        f'(ProductCommand("{LEVEL1_COMMAND}", handle_level1),)\n'
    )
    menu_import = "from services.tg_bot.src.menu import handle_level1\n"
    commands = _substitute(
        _bot_commands(),
        menu_import,
        "from services.tg_bot.src.handlers.notes import handle_note, handle_notes\n" + menu_import,
        where="notes bot command import",
    )
    commands = _substitute(
        commands,
        level1,
        "COMMANDS: tuple[ProductCommand, ...] = (\n"
        f'    ProductCommand("{LEVEL1_COMMAND}", handle_level1),\n'
        '    ProductCommand("note", handle_note),\n'
        '    ProductCommand("notes", handle_notes),\n'
        ")\n",
        where="notes bot command declarations",
    )
    bot = [one for one in bot_operations(marker) if one.path != BOT_COMMANDS]
    bot += [
        Operation("create", NOTES_HANDLERS, BOT_NOTES),
        Operation("replace", BOT_COMMANDS, commands),
    ]
    return backend, bot


def configure_notes(ctx):
    backend, bot = notes_operations(ctx["level1_marker"])
    ctx["task_description"] = (
        "Implement persistent caller-owned notes and the marker.\n" + render_change_set(backend)
    )
    ctx["followup_task_description"] = (
        "Register notes save/list and marker commands.\n" + render_change_set(bot)
    )
    ctx["level1_change_set_paths"] = [one.path for one in backend + bot]
    ctx["level1_brief"] = replace(
        ctx["level1_brief"],
        title="Notes bot",
        language="en",
        summary="Save and list persistent personal notes.",
        must_requirements=(
            {
                "id": "level1_setting",
                "text": "Persist notes and product marker.",
                "user_wording": "Keep my notes through deployments.",
            },
            {
                "id": "level1_command",
                "text": "Register /note, /notes and /level1.",
                "user_wording": "Let me save and list notes in Telegram.",
            },
        ),
        usage_examples=(
            {
                "requirement_id": "level1_command",
                "user_sends": "/note keep this",
                "product_answers": "Saved: keep this",
            },
            {
                "requirement_id": "level1_setting",
                "user_sends": "/notes",
                "product_answers": "keep this",
            },
        ),
    )
    ctx["level1_qa_criteria"] = (
        f"- GET /health returns 200\n- Stand mechanical notes: {ctx['level1_marker']}"
    )
    ctx["mechanical_acceptance"] = {"status": "running", "phase": "first_story"}
    return ctx
