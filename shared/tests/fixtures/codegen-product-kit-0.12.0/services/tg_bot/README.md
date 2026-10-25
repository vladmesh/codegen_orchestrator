# Telegram Bot

This directory contains a minimal python-telegram-bot application. Every Telegram
command and input handler is registered by the core command registry
(`src/generated/commands.py`). Unknown commands, and text when the product declares no text
handler, are answered by core in the core `language` setting.

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

Product commands, the non-command text handler and inline-button callbacks are declared in
`services/tg_bot/src/commands.py` in the forms described in `AGENTS.md` (Command registry); run
`make generate-from-spec` after changing them. `make lint` and generation fail closed on any
other form and on direct handler registration.

Background tasks and persistence layers stay product-owned; handler registration does not.
