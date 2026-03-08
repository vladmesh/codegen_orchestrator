# Agents Playbook

Инструкция для AI-ассистентов, работающих над этим проектом.

## Навигация

| Документ | Когда читать |
|----------|-------------|
| [docs/DEV_PIPELINE.md](docs/DEV_PIPELINE.md) | **ОБЯЗАТЕЛЬНО К ПРОЧТЕНИЮ** — жизненный цикл фичи и дата-дривен процесс |
| [docs/STATUS.md](docs/STATUS.md) | **Всегда первым** — текущая задача и контекст |
| [docs/backlog.md](docs/backlog.md) | Очередь задач, идеи (Read-only, генерируется из БД командой `make backlog`) |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | Перед изменением DTO, очередей, API |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Для понимания системы в целом |
| [docs/NODES.md](docs/NODES.md) | Описание агентов-узлов LangGraph |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Фазы и вехи |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Что уже сделано |

## Связанные проекты

- **service-template** (`/home/vlad/projects/service-template`) — фреймворк для генерации проектов

## Dev Pipeline

```
Идея → /brainstorm → БД Tasks → /plan → /implement → /e2e-run → /checkpoint
```

| Этап | Скилл | Артефакт |
|------|-------|----------|
| Исследование | `/brainstorm <topic>` | Запись в БД `brainstorms` (или markdown Spike) |
| Обнаружение | `/audit` | Находки → Создание Task в БД |
| Приоритизация | `/triage` | Создание новых Tasks в БД (API) |
| Декомпозиция | `/plan [#ID]` | `docs/plans/<task>.md` — шаги с Input/Output/Test |
| Реализация | `/implement [#ID]` | Код + тесты (TDD цикл по шагам плана) |
| Валидация | `/e2e-run` | `docs/e2e_results/<scenario>-<date>.md` |
| Фиксация | `/checkpoint` | CHANGELOG, ROADMAP, закрытие Task в БД |
| Аудит | `/audit` | Находки → Создание Task в БД |

Скиллы по умолчанию получают свой контекст (в т.ч. текущую задачу) **через API**, а не из старого файла `docs/STATUS.md`. Скиллы больше не работают с markdown файлами напрямую (кроме планов), а пишут и читают состояние через API.

**Планы не удаляются после реализации.** `/implement` дополняет план и отправляет API events с итерациями. `/checkpoint` удаляет план только если есть свежий E2E-результат.

**Код вне flow** допустим для мелких фиксов (< 3 файлов). Обязательно: запись в CHANGELOG + коммит с `[hotfix]` префиксом. Крупные изменения — только через flow.

## TDD Workflow (MANDATORY)

Red → Green → Refactor. Без исключений.

1. **Context**: прочитай `docs/STATUS.md` и `docs/CONTRACTS.md`
2. **Red**: напиши тест в `services/<service>/tests/{unit,integration}/`, убедись что падает
3. **Green**: минимальный код для прохождения теста
4. **Gate**: `make test-unit` + `make lint`. Обнови STATUS, CHANGELOG (backlog генерируется автоматически командой `make backlog`).

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

У части скиллов есть альтернативы для не-Claude агентов в `.agents/workflows/`.

| Skill | Описание |
|-------|----------|
| `/plan [#ID]` | Декомпозировать задачу на шаги, обновить Task в БД (plan) |
| `/implement [#ID]` | Взять задачу в работу (status: in_dev), TDD цикл, запись Task events |
| `/e2e-run <test> [--with-po] [--no-cleanup] [--feature]` | Запуск E2E теста (полный цикл: engineering → CI → deploy → verify, `--feature` пропускает scaffolding) |
| `/triage` | Разбор отчётов → создание новых задач через API |
| `/brainstorm <topic>` | Создание/обсуждение Brainstorm записи в БД |
| `/checkpoint` | Сбор статистики через API, обновление CHANGELOG/ROADMAP |
| `/audit` | Аудит кода → создание задачи в БД |
