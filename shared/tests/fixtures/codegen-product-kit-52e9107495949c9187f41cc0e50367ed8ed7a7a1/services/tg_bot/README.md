# Telegram Bot

This directory contains a minimal python-telegram-bot application. Every Telegram
command is registered by the core command registry (`src/generated/commands.py`): core
built-ins, product commands from `src/commands.py`, then bound module commands. Unknown
commands and text are answered by core in the core `language` setting.

## Development

Run `make setup` from the project root to create per-service venvs, then
start everything with `make dev-start`. The base Compose stack already
includes this service, so `make dev-start`

will build and start it automatically alongside the backend and database.

If you only need the bot (or want to force a rebuild), target the service
explicitly:

```bash
docker compose --project-directory . \
  -f infra/compose.base.yml \
  -f infra/compose.dev.yml \
  up --build tg_bot
```

The generated dev compose layer sets `TG_BOT_ALLOW_PLACEHOLDER_TOKEN=true`, so
the default placeholder token starts an idle container. Use this for smoke tests
without a real Telegram token, then set `TELEGRAM_BOT_TOKEN` before polling the
Telegram API.

To add a command, declare it in `services/tg_bot/src/commands.py` and run
`make generate-from-spec`:

```python
async def handle_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("pong")


COMMANDS = (ProductCommand("ping", handle_ping),)
```

`COMMANDS` is the only admitted product form: one module-level tuple of
`ProductCommand("literal-name", handler)` entries. `make lint` and generation fail closed,
with both source locations, on a name another owner claims (`/start`, `/command`, a module's
`/channel` or `/remind`), on any other declaration form, and on product code that mentions
`CommandHandler`, `MessageHandler`, `TypeHandler`, `PrefixHandler`, `ConversationHandler`,
`BaseHandler`, `add_handler` or `add_handlers`: product and module catch-alls cannot replace
the core unknown-input reply. The same refusal covers reading, assigning or deleting the
application's `handlers` registry, also through an alias such as `bot = context.application`.
Registered handlers are final at runtime: adding, removing or replacing one, or replacing the
`handlers` field itself, from product code raises; the bot is built with
`ApplicationBuilder().application_class(commands.CoreApplication)` in `src/main.py`. `commands.language(client_factory)` reads the core
language.

Background tasks and persistence layers stay product-owned; handler registration does not.
