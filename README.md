# Codegen Orchestrator

Мультиагентный оркестратор на базе LangGraph для автоматической генерации и деплоя проектов.

**Вход**: Описание проекта в Телеграме  
**Выход**: Работающий проект в продакшене (код, CI/CD, домен, SSL)

## Философия

- **Автономность**: Человек заходит раз в несколько дней, смотрит отчёты, докидывает деньги
- **Агенты как узлы графа**: Каждый агент — специалист со своими инструментами
- **Нелинейность**: Агенты могут вызывать друг друга в любом порядке
- **Spec-first**: Используем [service-template](https://github.com/vladmesh/service-template) для генерации кода

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                        Telegram Bot                             │
│                     (интерфейс пользователя)                    │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                       LangGraph Orchestrator                    │
│                                                                 │
│  CLI Agent (Product Owner)                                      │
│         │                                                       │
│         ▼                                                       │
│  ┌───────────────┐   ┌───────────────────────────────────────┐    │
│  │ Analyst →     │   │ Engineering Subgraph                      │    │
│  │ Zavhoz        │   │ Architect → Preparer → Developer → Tester │    │
│  └───────────────┘   └───────────────────────────────────────┘    │
│                                       │                          │
│                                       ▼                          │
│                         ┌───────────────────────────────────────┐    │
│                         │ DevOps Subgraph                                  │    │
│                         │ EnvAnalyzer → SecretResolver → ReadinessCheck → Deployer│    │
│                         └───────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                    │                    │
                    ▼                    ▼
        ┌───────────────────┐   ┌───────────────────┐
        │  service-template │   │    prod_infra     │
        │   (кодогенерация) │   │   (Ansible)       │
        └───────────────────┘   └───────────────────┘
```

## Связанные проекты

| Проект | Описание | Репо |
|--------|----------|------|
| **service-template** | Spec-first фреймворк для генерации микросервисов | [GitHub](https://github.com/vladmesh/service-template) |
| **prod_infra** | Ansible playbooks для настройки серверов | [GitHub](https://github.com/vladmesh/prod_infra) |

## Инфраструктура

- **LangGraph сервер**: Отдельный сервер для оркестратора и агентов
- **Prod серверы**: Управляются через prod_infra, используются для деплоя сгенерированных проектов
- **Телеграм**: Основной интерфейс для взаимодействия с человеком

## Development Setup

### Prerequisites
- Docker & Docker Compose
- Python 3.12+
- Git

### Quick Start

1. **Clone the repository**
   ```bash
   git clone https://github.com/vladmesh/codegen_orchestrator.git
   cd codegen_orchestrator
   ```

2. **Install git hooks** (for automatic code quality checks)
   ```bash
   make setup-hooks
   ```

3. **Set up environment**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

4. **Start services**
   ```bash
   make up
   make migrate
   make seed
   ```

5. **Run tests**
   ```bash
   make test-unit  # Fast unit tests
   make test-all   # All tests
   ```

### Development Workflow

- **Code quality**: Git hooks provide automatic quality enforcement
  - **Pre-commit**: Automatically formats code with `ruff format` and adds to commit (never blocks)
  - **Pre-push**: Runs linters and unit tests, BLOCKS push if either fails
- **Testing**: Write tests in `services/{service}/tests/{unit,integration}/`
- **CI/CD**: GitHub Actions runs tests on every push/PR
- **Skip hooks**: Use `--no-verify` flag if absolutely necessary (NOT recommended)

See [TESTING.md](docs/TESTING.md) for detailed testing guide.

## Документация

- [AGENTS.md](AGENTS.md) — описание каждого агента
- [ARCHITECTURE.md](ARCHITECTURE.md) — техническая архитектура
- [docs/LOGGING.md](docs/LOGGING.md) — руководство по структурированному логированию

## Logging

Проект использует `structlog` для структурированного логирования с поддержкой JSON-формата.

```python
from shared.logging_config import setup_logging
import structlog

setup_logging(service_name="my_service")
logger = structlog.get_logger()
logger.info("event_name", user_id=123, duration_ms=45.2)
```

**Environment variables:**
- `LOG_LEVEL` — уровень логирования (DEBUG, INFO, WARNING, ERROR)
- `LOG_FORMAT` — формат вывода: `json` (production) или `console` (dev)

Подробнее см. [docs/LOGGING.md](docs/LOGGING.md).

## Roadmap

См. [docs/backlog.md](docs/backlog.md) для актуального списка задач.

**Реализовано:**
- CLI Agent as Product Owner (pluggable: Claude Code, Factory.ai, custom)
- Engineering Subgraph (Architect → Preparer → Developer → Tester)
- DevOps Subgraph (LLM-based env analysis, auto-generates infra secrets)
- Session Management (Redis-based locks)
- Multi-tenancy (user_id propagation, project filtering)

**В бэклоге:**
- RAG с embeddings
- Telegram Bot Pool
- API Authentication