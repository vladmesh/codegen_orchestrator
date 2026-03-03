# Agents Playbook

Инструкция для AI-ассистентов, работающих над этим проектом.

## 🗺 Навигация

| Документ | Содержание |
|----------|------------|
| [README.md](README.md) | Обзор проекта, философия, архитектура |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Техническая архитектура, state schema, граф |
| [docs/NODES.md](docs/NODES.md) | Описание агентов-узлов LangGraph |
| [docs/backlog.md](docs/backlog.md) | Бэклог задач |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Фазы и вехи |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Что сделано |
| [docs/LOGGING.md](docs/LOGGING.md) | Структурированное логирование |
| [docs/TESTING.md](docs/TESTING.md) | Тестовая инфраструктура |

## 🚦 TDD Workflow (MANDATORY)

Мы работаем по строгому TDD процессу (Red -> Green -> Refactor).

### Шаг 1: Сбор контекста (Context)
Перед началом любой задачи:
1. Прочитай **`docs/STATUS.md`** — пойми текущую фазу и статус.
2. Прочитай **`docs/CONTRACTS.md`** — пойми контракты (DTO, очереди).

### Шаг 2: Red (Тесты)
1. Создай новый тест-файл в `services/<service>/tests/{unit,integration}/`.
2. Используй `make test-unit` для запуска.
3. Убедись, что **тест падает** (RED) с ожидаемой ошибкой (NotImplemented или AssertError).

### Шаг 3: Green (Реализация)
1. Напиши минимальный код для прохождения теста.
2. Запусти тесты снова → **GREEN**.

### Шаг 4: Milestone Gate (Финализация)
Когда задача выполнена:
1. Запусти полные тесты: `make test-unit`.
2. Запусти линтер: `make lint`.
3. Обнови docs: STATUS.md, CHANGELOG.md, backlog.md (см. `/implement` skill).
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
├── .claude/skills/     # Автоматизированные workflow (10 skills)
├── docs/               # Документация
│   ├── backlog.md      # Бэклог (Queue/Ideas/Done)
│   ├── ROADMAP.md      # Фазы и вехи
│   ├── CHANGELOG.md    # Что сделано
│   ├── STATUS.md       # Текущая задача
│   ├── USER_STORIES.md # Целевые сценарии
│   ├── NODES.md        # Описание агентов
│   ├── CONTRACTS.md    # DTO и очереди
│   ├── SECRETS.md      # Управление секретами
│   ├── TESTING.md      # Тестирование
│   ├── LOGGING.md      # Логирование
│   ├── ERROR_HANDLING.md # Обработка ошибок
│   └── GLOSSARY.md     # Терминология
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
│   ├── telegram_bot/   # Telegram interface (PO via Redis Streams)
│   ├── scheduler/      # Background jobs
│   ├── worker-manager/ # Docker lifecycle for CLI agents
│   ├── infra-service/  # Ansible provisioning
│   ├── engineering-worker/ # Engineering subgraph consumer
│   └── deploy-worker/     # DevOps subgraph consumer
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

### Добавление новых Tools (PO ReactAgent)

1. Создать Python функцию с `@tool` декоратором в `services/langgraph/src/po/tools.py`
2. Добавить tool в список tools в `services/langgraph/src/po/graph.py`
3. PO ReactAgent автоматически получит доступ к новому tool

### Добавление новых Tools (Developer Worker CLI)

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
make test-unit         # Unit тесты (быстрые, без зависимостей)
make test-integration  # Integration тесты (нужны DB/Redis)
```

## 🔧 Skills (`.claude/skills/`)

Автоматизированные workflow. Вызов: `/skill-name [args]`.

### Core loop
| Skill | Описание |
|-------|----------|
| `/next [#ID]` | Выбрать следующую задачу из backlog → STATUS.md |
| `/plan [#ID]` | Декомпозировать задачу на шаги с Input/Output/Test |
| `/implement [#ID]` | TDD цикл, обновление CHANGELOG/backlog/STATUS |

### Testing
| Skill | Описание |
|-------|----------|
| `/e2e-run [level] [scenario]` | Запуск E2E теста (Level A/B/C) |
| `/e2e-check [scenario]` | Проверить результат E2E прогона |
| `/e2e-cleanup [scenario]` | Очистить ресурсы после E2E |

### Meta & maintenance
| Skill | Описание |
|-------|----------|
| `/triage` | Разбор e2e-отчётов, brainstorms → backlog / service-template |
| `/brainstorm <topic>` | Создать/обсудить brainstorm документ |
| `/checkpoint` | Обновить CHANGELOG/ROADMAP/STATUS, рекомендовать следующую задачу |
| `/audit` | Аудит кода: dead code, smells, security, test gaps → backlog |

При работе над конкретной задачей загружай только релевантные файлы:

- **Новый агент**: `ARCHITECTURE.md`, `docs/NODES.md`, `services/langgraph/src/nodes/`
- **Новый tool**: `services/langgraph/src/tools/`, `services/langgraph/src/capabilities/__init__.py`
- **API endpoint**: `services/api/src/routers/`
- **Интеграция с service-template**: `/home/vlad/projects/service-template/`
- **Деплой**: `services/infra-service/`, `services/langgraph/src/subgraphs/devops/`
