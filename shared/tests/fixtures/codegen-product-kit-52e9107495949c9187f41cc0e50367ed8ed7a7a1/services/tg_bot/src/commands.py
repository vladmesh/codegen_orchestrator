"""Product-owned Telegram commands.

Declare each command as ``ProductCommand("name", handler)`` in ``COMMANDS`` and run
``make generate-from-spec``; the core registry then registers it after the core built-ins
and before bound module commands. Core owns /start, /command, unknown commands and text.
A name another owner claims (for example a module's /channel or /remind), any other
declaration form and direct handler registration fail generation and product CI.

Example::

    async def handle_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            await update.message.reply_text("pong")

    COMMANDS = (ProductCommand("ping", handle_ping),)
"""

from __future__ import annotations

from services.tg_bot.src.generated.commands import ProductCommand

COMMANDS: tuple[ProductCommand, ...] = ()
