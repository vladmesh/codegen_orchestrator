# AGENTS — Telegram Bot

## Overview

- Весь код и зависимости находятся в `services/tg_bot`. Держите `Dockerfile`, `src/` и `tests/` в этом каталоге.
- Запуск и тесты выполняются через `make` + Docker (`make dev-start`, `make tests tg_bot`). Не устанавливайте deps на хост.
- Скрипт запуска — `services/tg_bot/src/main.py`. Dockerfile использует uv и базовый образ `python:3.12-slim`.
## Event Publishing

Бот публикует события напрямую в Redis Streams (не через REST API backend'а):

- **Broker**: Lazy `get_broker()` из `shared.generated.events` — не импортируйте `broker` как атрибут
- **Events**: `command_received` для команд бота
- **Lifecycle**: Broker подключается при старте приложения (`post_init`) и отключается при shutdown (`post_shutdown`)

```python
from shared.generated.events import get_broker, publish_command_received
from shared.generated.schemas import CommandReceived

# Lifecycle: connect via post_init/post_shutdown hooks
async def post_init(application: Application) -> None:
    await get_broker().connect()

async def post_shutdown(application: Application) -> None:
    await get_broker().close()

# Publishing in handler:
event = CommandReceived(command=cmd, args=args, user_id=telegram_id, timestamp=datetime.now(UTC))
await publish_command_received(event)
```

## Import Rules

**PYTHONPATH** в Docker: `/app`

Все сервисы используют одинаковый PYTHONPATH. Два стиля импортов:

```python
# Внутри services/tg_bot/src/ — relative imports (предпочтительно)
from .main import build_application, handle_start
from .middleware import install_update_logging

# Absolute imports (тоже работают, но менее предпочтительны)
from services.tg_bot.src.main import handle_start
# Shared-пакет — всегда absolute import
from shared.generated.schemas import CommandReceived
from shared.generated.events import get_broker, publish_command_received
```

Если вы выносите команды или настройки в новые файлы, сначала создайте эти модули
в `services/tg_bot/src/`, например `handlers.py` или `config.py`, и только потом
импортируйте из них.

**Запрещено:**
```python
# НЕ ДЕЛАЙТЕ ТАК:
from src.main import ...                        # src — не пакет верхнего уровня
import main                                     # bare import, не работает как пакет
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `REDIS_URL` | Yes | Redis connection string (e.g., `redis://redis:6379`) |
| `BACKEND_API_URL` | Yes | Backend URL for Telegram identity resolution (e.g., `http://backend:8000`) |
| `USER_IDENTITY_CAPABILITY` | Yes | Generated secret that lets the bot call backend package routes as a Telegram user |

## Communication Patterns

1. **User Access** — HTTP GET to backend `/users/access` resolves the Telegram identity. Only a
   response with `status=active` is admitted; grants happen through the separately held backend
   capability and are never available to Telegram handlers.
2. **Command Events** — Direct publish to Redis Streams (события команд)
3. **Package Routes as the User** — Routes of installed backend packages (например `/reminders`)
   act only for the verified caller: the backend takes the owner from identity headers, never from
   a body or query. Call them only through
   `BackendClient.request_as_telegram_user(method, path, telegram_id, ...)` with the id from the real
   update (`update.effective_user.id`). It sends `X-Identity-Capability` (from
   `USER_IDENTITY_CAPABILITY`), `X-User-Channel: telegram` and `X-User-External-Id`, and fails closed
   without a valid id or capability. Never pass `user_ref` yourself, never send an identity the
   update did not carry, and never log the capability. A 401 means the bot is misconfigured; a 403
   means the user is unknown or inactive. The owner in responses and in `reminders.due` is
   `telegram:<id>`, so a due message goes to that chat.

   ```python
   async with BackendClient() as client:
       response = await client.request_as_telegram_user(
           "post",
           "/reminders",
           update.effective_user.id,
           json={"text": text, "remind_at": remind_at.isoformat()},
       )
   ```

## Dependencies

- `python-telegram-bot` — Telegram Bot API
- `faststream[redis]`, `redis`, `jsonschema` — Generated binding relay and action validation
- `httpx` — HTTP client for backend communication
- `shared` — Generated events and schemas from `shared/`

## Product Bindings

`services/tg_bot/bindings/*.yaml` and service settings manifests are product-owned.
`src/generated/bindings.py` and `binding_relay.py` are regenerated; edit bindings and run
`make generate-from-spec`, never handwritten reminders handlers. No binding means an inert
seed with no parser/relay startup. Bindings require both backend and tg_bot environments.

Use `kit bind reminders --default --product-root .` after installing reminders and textparse;
use `kit bind reminders --file /path/to/override.yaml --product-root .` for an explicit override.
Repeated defaults retain and refuse differing product edits. Bind declares the required
product timezone in the existing settings registry but never sets its value. Set it separately
through capability-protected `POST /settings/set`; handlers read product-scoped `/settings/get`
and fail without a valid IANA value. No timezone environment fallback or per-user value.
The product `language` (`ru`/`en`, product scope) is a core setting present in every fresh
product; bindings reference it, and neither a manifest nor bind may declare it again.

`kit check-install NAME --package-source DIR --json` (or `--catalog-source SRC --catalog-ref
REF`) previews an install read-only: `mechanical`, `glue` with file/line/owner/action items to
apply through product files, or `incompatible` with a stable reason code.

## Command registry

`src/generated/commands.py` is the only registration point: access in group -1, core `/start`
and `/command`, product `COMMANDS` from `src/commands.py`, bound module commands, then the core
unknown-input reply. Declare product commands only as `ProductCommand("name", handler)` entries
and run `make generate-from-spec`. Collisions, reserved names, other declaration forms and any
direct `add_handler`/`CommandHandler`/`MessageHandler` use, or any access to the application's
`handlers` registry (also through an alias such as `bot = context.application`), fail
generation and `make lint`. Keep `ApplicationBuilder().application_class(commands.CoreApplication)`
in `src/main.py`: registration refuses another application class and seals the registry field.
Callbacks keep bounded opaque context for ten minutes, tied to initiating user/chat/action,
and are consumed before any await. Restart/expiry requires rerunning the command. Commands
and callbacks always call `request_as_telegram_user` with the real update user, never an owner
from callback data. Generated mutations use one attempt to avoid automatic repeated effects.

The relay owns a separate subscriber broker, started/stopped through bot hooks. It consumes
declared Redis streams in `events:tg_bot` from first-start `0-0`, with persistent seven-day
completed-event dedupe and token-owned retryable claims. Transient sends remain pending;
blocked/missing chats and invalid recipients terminate with bounded logs. A crash/ambiguous
Telegram response before completion may duplicate a send; never promise exactly-once delivery.

## Доступ к боту

Доступ определяется только сохранённым `User.status`. `enforce_access` в группе `-1` до всех
обработчиков преобразует Telegram id в channel identity, запрашивает backend и пропускает обновление
только при `status=active`. Не добавляйте env allow-list, owner literal, temporary test identity или
публичный режим: неизвестные, inactive и malformed identities всегда отклоняются.
