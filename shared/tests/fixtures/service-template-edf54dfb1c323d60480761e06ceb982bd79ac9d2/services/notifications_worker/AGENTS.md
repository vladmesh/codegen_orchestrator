# AGENTS — Notifications Worker

## Overview

Event-driven FastStream worker, подписывающийся на Redis Streams и обрабатывающий события через `NotificationsController`.

- Весь код в `services/notifications_worker`. Не выносите файлы за пределы.
- Запуск и тесты только через `make` + Docker. Не устанавливайте deps на хост.
- Точка входа: `services/notifications_worker/src/main.py`.

## Import Rules

**PYTHONPATH** в Docker: `/app`

Используйте **fully qualified absolute imports** от корня проекта:

```python
# Внутри notifications_worker — fully qualified
from services.notifications_worker.src.controllers.notifications import NotificationsController
from services.notifications_worker.src.generated.protocols import NotificationsControllerProtocol

# Shared-пакет
from shared.generated.schemas import UserRegistered
from shared.generated.events import get_broker
```

**Запрещено:**
```python
# НЕ ДЕЛАЙТЕ ТАК:
from src.controllers.notifications import ...           # src — не пакет верхнего уровня
from notifications_worker.src.controllers import ...    # нет такого пакета
```

## Architecture

Сервис использует spec-first event adapter:

```
spec/notifications.yaml -> src/generated/ (protocols.py, event_adapter.py)
                                    |
                        src/controllers/notifications.py  <- ваш код
```

**Workflow при изменении событий:**
1. Отредактируйте `services/notifications_worker/spec/notifications.yaml`
2. Запустите `make generate-from-spec` (перегенерирует `src/generated/`)
3. Реализуйте новые методы в `src/controllers/notifications.py`

## Event Subscriptions

| Event | Handler |
|-------|---------|
| `user_granted` | `NotificationsController.on_user_granted` |

Подписки регистрируются автоматически через `create_event_adapter()` в `main.py`. Не регистрируйте `@broker.subscriber` вручную.

## Broker Pattern

Сервис использует канонический `get_broker()` из `shared.generated.events`, чтобы
издатели и подписчики работали с одним wire-форматом (`BinaryMessageFormatV1`) и
одним чтением `REDIS_URL`:

```python
from shared.generated.events import get_broker

broker = get_broker()
create_event_adapter(broker=broker, ...)
app = FastStream(broker)
await app.run()
```

`get_broker()` ленивый: он читает `REDIS_URL` и падает с `RuntimeError`, если переменная
не задана. Lifecycle (`connect`/`close`) управляется автоматически через `FastStream(broker).run()`.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `REDIS_URL` | Yes | Redis connection string (e.g., `redis://redis:6379`) |

## Key Commands

| Команда | Назначение |
|---------|-----------|
| `make tests notifications_worker` | Тесты сервиса |
| `make generate-from-spec` | Перегенерировать event adapter |
| `make lint` | Линтинг (включая spec compliance) |
| `make log notifications_worker` | Логи контейнера |
