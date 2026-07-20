# Agents Playbook

Инструкция для AI-ассистентов, работающих над этим проектом.

## Навигация

| Документ | Когда читать |
|----------|-------------|
| [docs/DEV_PIPELINE.md](docs/DEV_PIPELINE.md) | **ОБЯЗАТЕЛЬНО К ПРОЧТЕНИЮ** — жизненный цикл фичи и дата-дривен процесс |
| [docs/STATUS.md](docs/STATUS.md) | **Всегда первым** — текущая задача и контекст |
| [docs/backlog.md](docs/backlog.md) | Отложенный пул задач и идей (поддерживается вручную) |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | Перед изменением DTO, очередей, API |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Для понимания системы в целом |
| [docs/NODES.md](docs/NODES.md) | Описание агентов-узлов LangGraph |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Фазы и вехи |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Что уже сделано |

## Связанные проекты

- **service-template** (`/home/vlad/projects/service-template`) — фреймворк для генерации проектов

## Dev Pipeline

Задачи по разработке самого оркестратора заводятся и ведутся во внешнем пайплайне, а не в локальной Tasks DB. Локальный процесс — спринтовый, состояние живёт в markdown-файлах, которые поддерживаются вручную (генераторов под них нет). Полное описание: [docs/DEV_PIPELINE.md](docs/DEV_PIPELINE.md).

```
/go (диспетчер — читает docs/STATUS.md, первое совпадение выигрывает)
 ├─ нет спринта ───────────── /new-sprint
 ├─ фаза без задач ────────── /plan-phase
 ├─ есть задачи ───────────── /implement (по задаче, TDD)
 ├─ все задачи done ───────── /close-phase
 └─ все фазы done ─────────── endgame: /audit + /e2e-run → фиксы → /update-docs → /close-sprint
```

Скиллы получают контекст из `docs/STATUS.md` (текущий спринт и фаза) и работают с markdown-файлами спринта в `docs/sprints/NNN-slug/` напрямую.

**Код вне flow** допустим для мелких фиксов (< 3 файлов). Обязательно: запись в CHANGELOG + коммит с `[hotfix]` префиксом. Крупные изменения — только через flow.

## TDD Workflow (MANDATORY)

Red → Green → Refactor. Без исключений.

1. **Context**: прочитай `docs/STATUS.md` и `docs/CONTRACTS.md`
2. **Red**: напиши тест в `services/<service>/tests/{unit,integration}/`, убедись что падает
3. **Green**: минимальный код для прохождения теста
4. **Gate**: `make test-unit` + `make lint`. Обнови STATUS, CHANGELOG, backlog вручную по мере необходимости.

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
make test-service SERVICE=api # Per-service integration test
```

## Skills (`.claude/skills/`)

У части скиллов есть альтернативы для не-Claude агентов в `.agents/workflows/`.

| Skill | Описание |
|-------|----------|
| `/go` | Диспетчер: читает `docs/STATUS.md`, вызывает нужный скилл |
| `/new-sprint` | Создать спринт из VISION + ROADMAP + backlog |
| `/plan-phase` | Сгенерировать файлы задач для текущей фазы (с арх-гейтом) |
| `/implement` | TDD-цикл по одной задаче спринта, PR + CI + merge |
| `/close-phase` | Интеграционные тесты + переход к следующей фазе |
| `/close-sprint` | Финальный гейт: push, CHANGELOG, ROADMAP, история STATUS |
| `/audit` | Скан кода + проверка инвариантов VISION; находки → `docs/backlog.md` |
| `/e2e-run <test> [--with-po] [--no-cleanup] [--feature]` | E2E тест (engineering → CI → deploy → verify, `--feature` пропускает scaffolding) |
| `/test-maintenance` | Прогон/починка интеграционных тестов локально |
| `/brainstorm <topic>` | Структурированное обсуждение темы → `docs/brainstorms/<topic>.md` |
| `/update-docs` | Синхронизация живой документации с кодом |
| `/optimize` | Обработка фидбека по скиллам (`docs/skill-feedback.md`) и авто-улучшение |
| `/architect` | Декомпозиция Story → Tasks (для клиентских проектов, через API) |
| `/escort` | Сопровождение реального пользователя через полный пайплайн |
