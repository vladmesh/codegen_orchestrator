# Telegram Bot

This directory contains a minimal python-telegram-bot application. The
`main.py` file wires a `/start` handler and can be extended with more
commands, message handlers, or background jobs as you iterate.

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

To add a command, define an async handler in `services/tg_bot/src/main.py` and
register it in `build_application()`:

```python
async def handle_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("pong")


application.add_handler(CommandHandler("ping", handle_ping))
```

Feel free to add additional handlers, background tasks, or persistence
layers as needed.
