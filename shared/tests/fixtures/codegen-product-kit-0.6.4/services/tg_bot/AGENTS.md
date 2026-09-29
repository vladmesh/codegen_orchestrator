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

## Communication Patterns

1. **User Access** — HTTP GET to backend `/users/access` resolves the Telegram identity. Only a
   response with `status=active` is admitted; grants happen through the separately held backend
   capability and are never available to Telegram handlers.
2. **Command Events** — Direct publish to Redis Streams (события команд)

## Dependencies

- `python-telegram-bot` — Telegram Bot API
- `httpx` — HTTP client for backend communication
- `shared` — Generated events and schemas from `shared/`

## Доступ к боту

Доступ определяется только сохранённым `User.status`. `enforce_access` в группе `-1` до всех
обработчиков преобразует Telegram id в channel identity, запрашивает backend и пропускает обновление
только при `status=active`. Не добавляйте env allow-list, owner literal, temporary test identity или
публичный режим: неизвестные, inactive и malformed identities всегда отклоняются.
