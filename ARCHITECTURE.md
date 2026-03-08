# Архитектура

> **Актуально на**: 2026-03-09

## Обзор

Codegen Orchestrator — мультиагентная система для автоматической генерации и деплоя проектов. Пользователь описывает что хочет в Telegram → система создаёт, тестирует и деплоит.

## Технический стек

| Компонент | Технология |
|-----------|------------|
| **PO** | LangGraph ReactAgent (direct API/Redis tool calls) |
| **Developer Agents** | Claude Code, Factory.ai Droid via worker-manager (Docker + Redis) |
| **Backend Orchestration** | LangGraph (subgraphs) |
| **LLM** | Anthropic Claude (via CLI or API) |
| **Интерфейс** | Telegram Bot |
| **Кодогенерация** | service-template (Copier) |
| **Инфраструктура** | `services/infra-service` (Ansible) |
| **Хранение** | PostgreSQL + Redis |

## Ключевые концепции

### Planning Layer (Stories → Tasks → Runs)

Трёхуровневая абстракция для продуктового управления:
- **Story** — высокоуровневое требование от пользователя (через PO). Статусы: `draft` → `in_progress` → `completed`.
- **Task** — конкретная техническая задача. Статусы: `backlog` → `todo` → `in_dev` → `in_ci` → `testing` → `done`. Задачи могут иметь зависимости (`blocked_by_task_id`).
- **Run** — единица исполнения (engineering или deploy). Привязан к Task через `task_id`.

**Architect Node** (scheduler service) автоматически декомпозирует Story в Tasks:
1. PO создаёт Story и публикует `ArchitectMessage` в `architect:queue`
2. Architect Consumer вызывает LLM для декомпозиции story → N tasks с зависимостями
3. Task Dispatcher (каждые 30с) находит разблокированные tasks, создаёт Runs, публикует в `engineering:queue`
4. После завершения всех tasks — story автоматически завершается, триггерится deploy

Скиллы (`/plan`, `/implement`, `/triage`, `/checkpoint`) взаимодействуют с API для работы с бэклогом, сбора статистики и сохранения истории итераций (`TaskEvent`).
Файл `docs/backlog.md` является автогенерируемым (read-only) представлением базы данных (`make backlog`).

### Capabilities
Возможности Developer агента конфигурируются через `WorkerConfig.capabilities`:
- `git`, `github` — работа с репозиториями
- `python`, `node` — runtime environments
- Docker больше не предоставляется внутри контейнера (DinD удален). Инфраструктура поднимается через прокси-команды `orchestrator dev-env`.

## Сервисы

| Сервис | Описание |
|--------|----------|
| `api` | FastAPI + SQLAlchemy — проекты, серверы, users, configs |
| `telegram_bot` | Telegram интерфейс (PO via Redis Streams) |
| `worker-manager` | Docker контейнеры с CLI агентами и проксированием `docker compose` для sidecar-инфраструктуры (Flat Dev Environment). Воркеры работают в изолированной сети `codegen_worker`. |
| `langgraph` | Engineering/DevOps subgraphs. `engineering-worker` and `deploy-worker` are separate containers of the same image (Redis stream consumers, not independent services) |
| `scheduler` | Background workers: architect consumer (story→tasks LLM decomposition), task dispatcher (dispatch unblocked tasks, complete stories), github_sync, server_sync, health_checker |
| `infra-service` | Ansible runner, SSH операции (бывший infrastructure-worker) |

## Граф

```mermaid
graph TD
    User((User)) <--> |Telegram| Bot[Telegram Bot Service]
    Bot --> |"XADD po:input"| POInput[po:input stream]
    POInput --> POConsumer[PO Consumer]
    POConsumer --> PO[PO ReactAgent]
    PO --> |"XADD po:response:{req_id}"| POResp[po:response]
    POResp --> Bot

    PO --> |"tools: API calls"| API[API Service]
    PO --> |"XADD architect:queue"| ArchQueue[architect:queue]
    PO --> |"XADD deploy:queue"| DeployQueue[deploy:queue]
    PO -.-> |"po:proactive"| Bot

    API --> |"data"| DB[(PostgreSQL)]

    ArchQueue --> ArchConsumer[Architect Consumer<br/>scheduler]
    ArchConsumer --> |"LLM: story → tasks"| API
    ArchConsumer --> |"creates tasks with deps"| API

    Dispatcher[Task Dispatcher<br/>scheduler, 30s poll] --> |"finds unblocked tasks"| API
    Dispatcher --> |"XADD engineering:queue"| EngQueue[engineering:queue]
    Dispatcher --> |"story complete → XADD deploy:queue"| DeployQueue

    EngQueue --> EngConsumer[Engineering Consumer]
    EngConsumer --> EngGraph[Engineering Subgraph]

    DeployQueue --> DepConsumer[Deploy Consumer]
    DepConsumer --> DepGraph[DevOps Subgraph]

    %% Feedback Loops
    EngGraph --> |"task done → API"| API
    DepGraph --> |"system events → po:input"| POInput
    Dispatcher -.-> |"story completed → po:proactive"| Bot
```

### Потоки данных

```
User → Telegram Bot → XADD po:input {type, user_id, request_id, text}
                                  │
                                  ▼
                       PO ReactAgent (langgraph)
                       │  • Python @tool functions
                       │  • PostgreSQL checkpointer (per-user thread)
                       │  • Reminder poller
                       │
                       ├──► API (create_project, set_secret, create_story, ...)
                       ├──► XADD architect:queue → Architect Consumer (scheduler)
                       │                              │ LLM decomposition
                       │                              ▼
                       │                           API: create tasks with blocked_by chains
                       │                              │
                       │                              ▼
                       │                           Task Dispatcher (scheduler, 30s poll)
                       │                              ├──► XADD engineering:queue → Engineering Subgraph
                       │                              └──► story complete → XADD deploy:queue + po:proactive
                       ├──► XADD deploy:queue → DevOps Subgraph
                       └──► XADD po:response:{request_id} {text}
                                  │
                                  ▼
                       Telegram Bot → User

Engineering completion → API (task done) → Dispatcher picks next unblocked task
All tasks done → Dispatcher completes story → deploy + PO notification
```

**Key Features:**
- **PO ReactAgent**: LangGraph agent with native Python tools, PostgreSQL checkpointer
- **Developer Workers**: CLI agents (Claude Code, Factory.ai) in Docker containers via worker-manager. Network isolated (`codegen_worker` network) to prevent access to orchestrator DBs.
- **Engineering Subgraph**: Repo creation → Scaffold (copier via worker-manager) → Developer → CI gate (max 3 fix iterations)
- **DevOps Subgraph**: LLM-based env analysis, env groups for coherent secrets, Ansible deployment via infra-service
- **Unified Redis Consumers**: All 9 consumers use `RedisStreamClient.consume()` with PEL recovery (`claim_pending=True`) — crashed messages are automatically re-delivered on restart. See [CONTRACTS.md](docs/CONTRACTS.md#consumer-patterns)

## Внешние зависимости

| Репозиторий | Использование |
|-------------|---------------|
| [service-template](https://github.com/project-factory-organization/service-template) | Copier шаблон для генерации проектов |

## Документация

Детальная документация вынесена в отдельные файлы:

| Тема | Файл |
|------|------|
| **Contracts (DTO)** | [docs/CONTRACTS.md](docs/CONTRACTS.md) |
| **Glossary** | [docs/GLOSSARY.md](docs/GLOSSARY.md) |
| **Error Handling** | [docs/ERROR_HANDLING.md](docs/ERROR_HANDLING.md) |
| **Secrets** | [docs/SECRETS.md](docs/SECRETS.md) |
| Status & Progress | [docs/STATUS.md](docs/STATUS.md) |
| Resource Management | [docs/resource-management.md](docs/resource-management.md) |
| Coding Agents (Claude/Droid) | [docs/coding-agents.md](docs/coding-agents.md) |
| Parallel Workers | [docs/parallel-workers.md](docs/parallel-workers.md) |
| Logging | [docs/LOGGING.md](docs/LOGGING.md) |


## Мониторинг

### LangSmith

```bash
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=...
```

### Логирование

Все сервисы используют `structlog` (JSON для prod, console для dev).
Подробнее: [docs/LOGGING.md](docs/LOGGING.md)
