# Agents Playbook

Точка входа для AI-агентов, работающих с этим проектом.

## С чего начать

1. **`TASK.md`** — описание задачи проекта
2. **`ARCHITECTURE.md`** — архитектура и spec-first подход
3. **`CONTRIBUTING.md`** — правила кодирования (enforced CI)
4. **`services.yml`** — реестр всех сервисов

## Команды

Все операции выполняются через `make`. Никогда не запускайте инструменты напрямую.

| Команда | Назначение |
|---------|-----------|
| `make setup` | Bootstrap: venvs, codegen, git hooks (один раз) |
| `make lint` | Полный lint gate: ruff format/check, xenon, spec checks, controller sync, deptry |
| `make format` | Авто-форматирование (ruff) |
| `make typecheck` | mypy по сервисам |
| `make tests` | Все тесты |
| `make tests <service>` | Тесты одного сервиса |
| `make dev-start` / `make dev-stop` | Docker dev-окружение |
| `make prod-start` / `make prod-stop` | Docker prod-окружение |
| `make log <service>` | Логи контейнера |
| `make generate-from-spec` | Перегенерировать код из YAML-спеков |
| `make validate-specs` | Проверить YAML-спеки до генерации |
| `make lint-controllers` | Проверить синхронизацию контроллеров с протоколами |
| `make migrate` | Применить все Alembic-миграции (`upgrade head`) |
| `make makemigrations name="..."` | Создать Alembic-миграцию |
| `make openapi` | Экспорт OpenAPI JSON |
| `make typescript` | Генерация TypeScript типов |
| `make test-integration` | Интеграционные тесты в Docker |

Правила Docker Compose окружения, service DNS, healthchecks и переменных БД/Redis описаны в `infra/README.md`.

`make lint` always runs the same gate sequence: `ruff format --check`,
`ruff check`, xenon with `--max-absolute B --max-modules A --max-average A`,
spec validation, spec compliance, controller sync, then `make check-deps`.
`check-deps` runs deptry for each service that has it installed in its service
venv.

## Required application environment variables

Не задавайте fallback для обязательных runtime-настроек приложения. Если переменная отсутствует,
приложение должно упасть с понятной ошибкой:

```python
value = os.getenv("REQUIRED_VAR")
if not value:
    raise RuntimeError("REQUIRED_VAR is not set; please add it to your environment variables")
```

Все обязательные переменные документируются в `.env.example`. Явные локальные defaults допустимы
в Compose interpolation и изолированных тестовых fixtures.

## Read-Only зоны

Не редактируйте вручную:
- `shared/shared/generated/` — перегенерируется через `make generate-from-spec`
- `services/*/src/generated/` — перегенерируется по спекам сервиса
- `.framework/` — внутренности фреймворка

## Брокер событий — паттерн get_broker()

`shared/shared/generated/events.py` предоставляет lazy-брокер через `get_broker()`.
**Не** импортируйте `broker` как атрибут модуля.

**Подключение (FastAPI lifespan):**
```python
from shared.generated.events import get_broker

@asynccontextmanager
async def lifespan(app: FastAPI):
    broker = get_broker()
    await broker.connect()
    yield
    await broker.close()
```

**Публикация событий:**
```python
from shared.generated.events import publish_command_received
from shared.generated.schemas import CommandReceived

event = CommandReceived(...)
await publish_command_received(event)  # broker должен быть подключён
```
**FastStream workers** (`python-faststream`) создают `RedisBroker` напрямую в `main()` и передают в `create_event_adapter()`. Lifecycle управляется через `FastStream(broker).run()`.

## Документация сервисов
- **Backend API:** `services/backend/AGENTS.md`
- **Telegram Bot:** `services/tg_bot/AGENTS.md`
- **Notifications Worker:** `services/notifications_worker/AGENTS.md`
- **Frontend:** `services/frontend/AGENTS.md`
