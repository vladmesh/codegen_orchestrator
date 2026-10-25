"""Product-owned Telegram commands, text handler and inline-button callbacks.

The admitted forms are described in ``services/tg_bot/AGENTS.md`` (Command registry); run
``make generate-from-spec`` after changing them.
"""

from __future__ import annotations

from services.tg_bot.src.generated.commands import ProductCommand

COMMANDS: tuple[ProductCommand, ...] = ()
