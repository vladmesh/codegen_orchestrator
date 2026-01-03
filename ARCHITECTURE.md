# Архитектура

## Обзор

Codegen Orchestrator — это мультиагентная система на базе LangGraph, где каждый агент является узлом графа со своими инструментами. Агенты могут вызывать друг друга нелинейно для решения сложных задач.

## Технический стек

| Компонент | Технология |
|-----------|------------|
| Оркестрация | LangGraph |
| LLM | OpenAI / Anthropic / OpenRouter |
| Интерфейс | Telegram Bot |
| Кодогенерация | service-template (Copier) + Factory.ai Droid |
| Инфраструктура | prod_infra (Ansible) |
| Хранение состояния | PostgreSQL + Redis |
| Секреты | project.config.secrets (PostgreSQL) |

## State Schema

Глобальное состояние графа определено в [graph.py](services/langgraph/src/graph.py) как `OrchestratorState(TypedDict)`.

**Основные группы полей:**
- **Messages** — история сообщений
- **Project Context** — `current_project`, `project_spec`, `po_intent`
- **Dynamic PO** — `thread_id`, `active_capabilities`, `awaiting_user_response`
- **Repository** — `repo_info`, `project_complexity`, `architect_complete`
- **Engineering Subgraph** — `engineering_status`, `test_results`, `engineering_iterations`
- **DevOps Subgraph** — `provided_secrets`, `missing_user_secrets`, `deployment_result`
- **User Context** — `telegram_user_id`, `user_id`

## Сервисы

| Сервис | Описание | Порт |
|--------|----------|------|
| `api` | FastAPI + SQLAlchemy, хранит проекты/серверы/agent_configs | 8000 |
| `langgraph` | LangGraph worker, обрабатывает messages через Dynamic PO | - |
| `telegram_bot` | Telegram интерфейс | - |
| `workers-spawner` | Спавнит agent контейнеры через Redis API | - |
| `scheduler` | Фоновые задачи (github_sync, server_sync, health_checker) | - |
| `preparer` | Copier runner для scaffolding проектов | - |
| `deploy-worker` | Консьюмер deploy:queue, запускает DevOps subgraph | - |
| `infrastructure-worker` | Provisioning серверов, Ansible runner, SSH операции | - |
| `infrastructure` | Ansible playbooks для настройки серверов | - |

## Граф (CLI Agent Architecture)

```
┌─────────┐     ┌──────────────────────┐     ┌─────────────────────────────┐
│  START  │────▶│ Telegram Bot         │────▶│  workers-spawner            │
└─────────┘     │                      │     │  (Docker isolation)         │
                └──────────────────────┘     └──────────┬──────────────────┘
                                                        │
                                                        ▼
                                             ┌────────────────────────────┐
                                             │ CLI Agent (Product Owner)  │
                                             │ (Claude/Factory/custom)    │
                                             │                            │
                                             │ • All API tools available  │
                                             │ • Native tool calling      │
                                             │ • Session via Redis        │
                                             └──────────┬─────────────────┘
                                                        │
                                                        ▼ (tool calls)
                ┌───────────────────────────────────────┴────────────────────────────┐
                │                                       │                            │
                ▼                                       ▼                            ▼
┌───────────────────────────┐         ┌─────────────────────────┐   ┌──────────────────────┐
│ Analyst (delegate_analyst)│         │ Engineering Subgraph    │   │ DevOps Subgraph      │
│    │                      │         │ (trigger_engineering)   │   │ (trigger_deploy)     │
│    ▼                      │         └──────────┬──────────────┘   └──────────┬───────────┘
│ ┌──────────────────┐      │                    │                             │
│ │  Create Project  │      │                    ▼                             ▼
│ └────────┬─────────┘      │    ┌────────────────────────────┐  ┌──────────────────────────┐
│          │                │    │ Architect → Preparer →     │  │ EnvAnalyzer → Deployer   │
└──────────┼────────────────┘    │ Developer → Tester         │  │ (Ansible via infra-worker)│
           │                     │ (max 3 iterations)         │  └──────────────────────────┘
           ▼                     └────────────────────────────┘
   ┌───────────────┐
   │    Zavhoz     │
   │  (resources)  │
   └───────────────┘
```

**Key Features:**
- **CLI Agent**: Product Owner as pluggable CLI worker (Claude Code, Factory.ai, custom)
- **Native Tools**: All API endpoints exposed as tools via OpenAPI
- **Session Management**: Redis-based locks (PROCESSING/AWAITING states)
- **Engineering Subgraph**: Architect → Preparer → Developer → Tester (max 3 iterations)
- **DevOps Subgraph**: LLM-based env analysis, Ansible deployment via infrastructure-worker

## Внешние зависимости

| Репозиторий | Использование |
|-------------|---------------|
| [service-template](https://github.com/vladmesh/service-template) | Copier шаблон для генерации проектов |
| [prod_infra](https://github.com/vladmesh/prod_infra) | Ansible playbooks для деплоя |

## Документация

Детальная документация вынесена в отдельные файлы:

| Тема | Файл |
|------|------|
| Resource Management (Завхоз) | [docs/resource-management.md](docs/resource-management.md) |
| Coding Agents (Claude/Droid) | [docs/coding-agents.md](docs/coding-agents.md) |
| Parallel Workers | [docs/parallel-workers.md](docs/parallel-workers.md) |
| Logging | [docs/LOGGING.md](docs/LOGGING.md) |
| Nodes | [docs/NODES.md](docs/NODES.md) |
| Testing | [docs/TESTING.md](docs/TESTING.md) |

## Мониторинг

### LangSmith

```bash
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=...
```

### Логирование

Все сервисы используют `structlog` (JSON для prod, console для dev).
Подробнее: [docs/LOGGING.md](docs/LOGGING.md)

## Открытые вопросы

### Решено

1. ~~**Ресурсница**: отдельный сервис или часть оркестратора?~~ → **Узел LangGraph** (Zavhoz)
2. ~~**Хранение секретов**~~ → **project.config.secrets** через PostgreSQL API
3. ~~**Coding agents**: писать свои или использовать готовые?~~ → **Factory.ai Droid**
4. ~~**Docker-in-Docker для тестов**~~ → **Sysbox** (безопасный nested Docker)
5. ~~**Session management**~~ → **Redis-based locks** (PROCESSING/AWAITING states)

### В бэклоге

- **Persistent Checkpointing** — сейчас используется `MemorySaver`, миграция на `PostgresSaver` запланирована
- Human escalation (backlog: Human Escalation)
- Cost tracking (backlog: Cost Tracking)
- RAG с embeddings (backlog: RAG с Embeddings)
- Telegram Bot Pool (backlog: Telegram Bot Pool)
- API Authentication (backlog: API Authentication)
