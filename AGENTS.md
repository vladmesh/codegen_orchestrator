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

## 🚦 TDD Workflow (MANDATORY)

Мы работаем по строгому TDD процессу (Red -> Green -> Refactor).

### Шаг 1: Сбор контекста (Context)
Перед началом любой задачи:
1. Прочитай **`docs/STATUS.md`** — пойми текущую фазу и статус.
2. Прочитай **`docs/CONTRACTS.md`** — пойми контракты (DTO, очереди).

### Шаг 2: Изучение Legacy (Reference)
1. Если есть старые тесты в `tests_legacy/` — изучи их для идей.
2. **НЕ копируй** их бездумно — архитектура изменилась.
3. Используй их как источник вдохновения для тест-кейсов.

### Шаг 3: Red (Тесты)
1. Создай новый тест-файл в `services/<service>/tests/{unit,service,integration}/`.
2. Используй `make test-<service>-...` для запуска.
3. Убедись, что **тест падает** (RED) с ожидаемой ошибкой (NotImplemented или AssertError).

### Шаг 4: Green (Реализация)
1. Напиши минимальный код для прохождения теста.
2. Запусти тесты снова → **GREEN**.

### Шаг 5: Milestone Gate (Финализация)
Когда задача (P*.*) выполнена:
1. Запусти полные тесты сервиса: `make test-<service>`.
2. Обнови **`docs/STATUS.md`**: отметь пункт как выполненный (✅).
3. Сделай коммит: `git commit -m "feat: implement P*.* <name>"`.
4. Если есть сомнения или нужно менять контракты (`shared/contracts`) — **STOP** и запроси ревью у пользователя.

⚠️ **Review Trigger**: Если задача требует изменения `Shared Contracts` или схемы БД, которые не описаны в плане — остановись и спроси.

См. подробности в [TESTING.md](docs/TESTING.md).

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
│   ├── telegram_bot/   # Telegram interface + PO sessions
│   ├── scheduler/      # Background jobs
│   ├── worker-manager/ # Docker lifecycle for CLI agents
│   ├── scaffolder/     # Copier runner (project scaffolding)
│   └── infra-service/  # Ansible provisioning
├── packages/
│   ├── orchestrator-cli/ # CLI tools for agents
│   └── worker-wrapper/   # Agent container entrypoint
├── shared/             # Shared code between services
│   ├── contracts/     # DTOs and queue schemas
│   ├── models/         # SQLAlchemy models
│   └── *.py            # Utilities
└── tests/              # E2E tests
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
- **Деплой**: `services/infra-service/`, `services/langgraph/src/subgraphs/devops/`
