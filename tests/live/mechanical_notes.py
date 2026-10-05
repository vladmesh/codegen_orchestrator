"""First-story notes customization for the mechanical stand, before any install."""

from dataclasses import replace

from level1_change_set import (
    BACKEND_ROUTER,
    BOT_MAIN,
    Operation,
    _backend_router,
    _bot_main,
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
BACKEND_NOTES = '''"""Product-owned notes, scoped to the core-verified caller."""
import os
from typing import Annotated
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
        await redis.rpush("notes:" + owner, note.text)
    return note

@router.get("/notes")
async def list_notes(owner: Caller) -> list[str]:
    async with Redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as redis:
        notes: list[str] = await redis.lrange("notes:" + owner, 0, -1)
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


def notes_operations(marker):
    backend = [one for one in backend_operations(marker) if one.path != BACKEND_ROUTER]
    backend += [
        Operation("create", NOTES_ROUTER, BACKEND_NOTES),
        Operation(
            "replace",
            BACKEND_ROUTER,
            _backend_router() + "\nfrom .routers.notes import router as notes_router\n"
            "api_router.include_router(notes_router)\n",
        ),
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
    bot = [one for one in bot_operations(marker) if one.path != BOT_MAIN]
    bot += [
        Operation("create", NOTES_HANDLERS, BOT_NOTES),
        Operation(
            "replace",
            BOT_MAIN,
            _substitute(
                _bot_main(),
                "    bindings.register(application, BackendClient)",
                "    from services.tg_bot.src.handlers.notes import handle_note, handle_notes\n"
                '    application.add_handler(CommandHandler("note", handle_note))\n'
                '    application.add_handler(CommandHandler("notes", handle_notes))\n'
                "    bindings.register(application, BackendClient)",
                where="notes bot handler registration",
            ),
        ),
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
                "user_input": "/note keep this",
                "expected_result": "Saved: keep this",
            },
            {
                "requirement_id": "level1_setting",
                "user_input": "/notes",
                "expected_result": "keep this",
            },
        ),
    )
    ctx["level1_qa_criteria"] = (
        f"- GET /health returns 200\n- Stand mechanical notes: {ctx['level1_marker']}"
    )
    ctx["mechanical_acceptance"] = {"status": "running", "phase": "first_story"}
    return ctx
