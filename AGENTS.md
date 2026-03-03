# Agents Playbook

Инструкция для AI-ассистентов, работающих над этим проектом.

## Навигация

| Документ | Когда читать |
|----------|-------------|
| [docs/STATUS.md](docs/STATUS.md) | **Всегда первым** — текущая задача и контекст |
| [docs/backlog.md](docs/backlog.md) | Очередь задач, идеи |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | Перед изменением DTO, очередей, API |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Для понимания системы в целом |
| [docs/NODES.md](docs/NODES.md) | Описание агентов-узлов LangGraph |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Фазы и вехи |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Что уже сделано |

## Связанные проекты

- **service-template** (`/home/vlad/projects/service-template`) — фреймворк для генерации проектов

## Dev Pipeline

```
Идея → /brainstorm → backlog → /next → /plan → /implement → /e2e-run → /checkpoint
```

| Этап | Скилл | Артефакт |
|------|-------|----------|
| Исследование | `/brainstorm <topic>` | `docs/brainstorms/<topic>.md` |
| Приоритизация | `/triage` или вручную | `docs/backlog.md` — новый item |
| Взять в работу | `/next [#ID]` | `docs/STATUS.md` — Current Task |
| Декомпозиция | `/plan [#ID]` | `docs/plans/<task>.md` — шаги с Input/Output/Test |
| Реализация | `/implement [#ID]` | Код + тесты (TDD цикл по шагам плана) |
| Валидация | `/e2e-run` → `/e2e-check` | `docs/e2e_results/<scenario>-<date>.md` |
| Фиксация | `/checkpoint` | CHANGELOG, ROADMAP, STATUS |
| Аудит | `/audit` | Находки → backlog |

Без аргумента скиллы берут текущую задачу из `docs/STATUS.md`.

**Планы не удаляются после реализации.** `/implement` дополняет план отклонениями, `/checkpoint` удаляет только если есть свежий E2E-результат.

**Код вне flow** допустим для мелких фиксов (< 3 файлов). Обязательно: запись в CHANGELOG + коммит с `[hotfix]` префиксом. Крупные изменения — только через flow.

## TDD Workflow (MANDATORY)

Red → Green → Refactor. Без исключений.

1. **Context**: прочитай `docs/STATUS.md` и `docs/CONTRACTS.md`
2. **Red**: напиши тест в `services/<service>/tests/{unit,integration}/`, убедись что падает
3. **Green**: минимальный код для прохождения теста
4. **Gate**: `make test-unit` + `make lint`. Обнови STATUS, CHANGELOG, backlog.

**Review Trigger**: изменение `shared/contracts/` или схемы БД, не описанное в плане → **STOP**, спроси пользователя.

## Правила

**Переменные окружения** — никогда не используй default values:
```python
# Wrong
api_key = os.getenv("OPENAI_API_KEY", "sk-test")

# Correct
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not set")
```

**Логирование** — `structlog` везде, никогда `print()`:
```python
from shared.log_config import setup_logging
import structlog
setup_logging(service_name="my_service")
logger = structlog.get_logger()
```

**LangGraph узлы** — state как TypedDict, возвращать dict:
```python
async def my_node(state: OrchestratorState) -> dict:
    return {"messages": [...], "current_agent": "my_node"}
```

## Makefile

```bash
make up / down / build       # Docker lifecycle
make migrate                 # Run DB migrations
make lint                    # Ruff linter
make format                  # Ruff formatter
make test-unit               # Unit tests (fast, no deps)
make test-integration        # Integration tests (require DB/Redis)
make test-{service}-unit     # Per-service: api, langgraph, scheduler, telegram
```

## Skills (`.claude/skills/`)

| Skill | Описание |
|-------|----------|
| `/next [#ID]` | Выбрать задачу из backlog → STATUS.md |
| `/plan [#ID]` | Декомпозировать задачу на шаги |
| `/implement [#ID]` | TDD цикл, обновление артефактов |
| `/e2e-run [level] [scenario]` | Запуск E2E теста (Level A/B/C) |
| `/e2e-check [scenario]` | Проверить результат E2E |
| `/e2e-cleanup [scenario]` | Очистить ресурсы после E2E |
| `/triage` | Разбор отчётов → backlog / service-template |
| `/brainstorm <topic>` | Brainstorm документ |
| `/checkpoint` | Обновить CHANGELOG/ROADMAP/STATUS |
| `/audit` | Аудит кода → backlog |
