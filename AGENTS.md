# Agents Playbook

Инструкция для AI-ассистентов, работающих над этим проектом.

## 🗺 Навигация

| Документ | Содержание |
|----------|------------|
| [README.md](README.md) | Обзор проекта, философия, архитектура |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Техническая архитектура, state schema, граф |
| [docs/NODES.md](docs/NODES.md) | Описание агентов-узлов LangGraph |
| [docs/backlog.md](docs/backlog.md) | Бэклог задач и roadmap |
| [docs/LOGGING.md](docs/LOGGING.md) | Структурированное логирование |
| [docs/TESTING.md](docs/TESTING.md) | Тестовая инфраструктура |
| [docs/new_architecture/tests/TESTING_STRATEGY.md](docs/new_architecture/tests/TESTING_STRATEGY.md) | Новая стратегия тестирования (4 уровня) |

## 🚦 TDD Workflow (MANDATORY)

Мы работаем по строгому TDD процессу (Red -> Green -> Refactor).
Любая новая функциональность должна начинаться с тестов.

1.  **RED (Integration)**: Напиши "service" тест (`docker/test/service/`), который падает.
2.  **RED (Unit)**: Напиши unit тест (`services/<service>/tests/unit/`), который падает.
3.  **GREEN**: Реализуй минимальный код для прохождения тестов.
4.  **REFACTOR**: Улучши код, не ломая тесты.

См. подробности в [TESTING_STRATEGY.md](docs/new_architecture/tests/TESTING_STRATEGY.md).

## 🛠 Технический стек

| Компонент | Технология |
|-----------|------------|
| Язык | Python 3.12 |
| Оркестрация | LangGraph |
| LLM | OpenAI / Anthropic / OpenRouter |
| Интерфейс | python-telegram-bot |
| Database | PostgreSQL |
| Cache | Redis |

## 📂 Структура проекта

```
codegen_orchestrator/
├── README.md           # Обзор проекта
├── AGENTS.md           # Этот файл
├── ARCHITECTURE.md     # Техническая архитектура
├── CLAUDE.md           # Инструкции для Claude Code
├── docs/               # Документация
│   ├── NODES.md        # Описание агентов
│   ├── LOGGING.md      # Логирование
│   ├── TESTING.md      # Тестирование
│   └── backlog.md      # Бэклог
├── services/
│   ├── api/            # FastAPI backend
│   │   └── src/        # routers, models, services
│   ├── langgraph/      # LangGraph worker
│   │   └── src/
│   │       ├── nodes/          # Agent nodes
│   │       ├── tools/          # LangChain tools
│   │       ├── capabilities/   # Capability registry
│   │       ├── subgraphs/      # Engineering, DevOps
│   │       └── schemas/        # State schemas
│   ├── telegram_bot/   # Telegram interface
│   ├── scheduler/      # Background jobs
│   ├── workers-spawner/ # CLI agent container spawner
│   ├── universal-worker/ # Base image for CLI agents
│   ├── preparer/       # Copier runner
│   └── infrastructure/ # Ansible playbooks
├── shared/             # Shared code between services
│   ├── models/         # SQLAlchemy models
│   └── *.py            # Utilities
└── tests/              # E2E tests (future)
```

## 🔗 Связанные проекты

При работе над оркестратором часто нужен контекст из:

- **service-template** (`/home/vlad/projects/service-template`) — фреймворк для генерации проектов

## ⚠️ CRITICAL: Правила работы

### Переменные окружения

**НИКОГДА не используй default values:**

```python
# ❌ Плохо
api_key = os.getenv("OPENAI_API_KEY", "sk-test")

# ✅ Хорошо
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not set")
```

### LangGraph узлы

Каждый агент — async функция, работающая со state:

```python
from .schemas.orchestrator import OrchestratorState

async def my_node(state: OrchestratorState) -> dict:
    # Логика агента
    return {"messages": [...], "current_agent": "my_node"}
```

### Добавление нового агента

1. Создать файл в `services/langgraph/src/nodes/<name>.py`
2. Базовый класс: `LLMNode` (agentic) или функция (functional)
3. Добавить узел в граф (`services/langgraph/src/graph.py`)
4. Добавить рёбра и routing логику
5. Если нужны tools — создать в `services/langgraph/src/tools/`
6. Если нужна capability — добавить в `services/langgraph/src/capabilities/__init__.py`
7. Описать агента в `docs/NODES.md`
8. Добавить тесты в `services/langgraph/tests/unit/`

### Добавление новых Tools (CLI Agent)

1. Создать API endpoint в `services/api/src/routers/`
2. Зарегистрировать tool в OpenAPI schema (автоматически через FastAPI)
3. Claude Code CLI автоматически получит доступ к новому tool через API discovery

## 🔄 Makefile команды

```bash
make build      # Собрать Docker образы
make up         # Запустить все сервисы
make down       # Остановить сервисы
make logs       # Посмотреть логи
make format     # Форматирование кода
make lint       # Линтеры
make test       # Все тесты
make test-unit  # Только unit тесты (быстрые)
```

## 🧠 Контекст при работе

При работе над конкретной задачей загружай только релевантные файлы:

- **Новый агент**: `ARCHITECTURE.md`, `docs/NODES.md`, `services/langgraph/src/nodes/`
- **Новый tool**: `services/langgraph/src/tools/`, `services/langgraph/src/capabilities/__init__.py`
- **API endpoint**: `services/api/src/routers/`
- **Интеграция с service-template**: `/home/vlad/projects/service-template/`
- **Деплой**: `services/infrastructure/`, `services/langgraph/src/subgraphs/devops.py`
