# Архитектура

> **Актуально на**: 2026-01-09

## Обзор

Codegen Orchestrator — мультиагентная система для автоматической генерации и деплоя проектов. Пользователь описывает что хочет в Telegram → система создаёт, тестирует и деплоит.

## Технический стек

| Компонент | Технология |
|-----------|------------|
| **CLI Agents** | Claude Code, Factory.ai Droid |
| **Agent Orchestration** | workers-spawner (Docker + Redis) |
| **Backend Orchestration** | LangGraph (subgraphs) |
| **LLM** | Anthropic Claude (via CLI or API) |
| **Интерфейс** | Telegram Bot |
| **Кодогенерация** | service-template (Copier) |
| **Инфраструктура** | `services/infrastructure-worker/ansible` |
| **Хранение** | PostgreSQL + Redis |

## Ключевые концепции

### Headless Mode
CLI агенты работают в headless режиме — чистый JSON ввод/вывод без TUI. Это обеспечивает надёжный парсинг и session continuity.

### Capabilities
Возможности агента конфигурируются через `WorkerConfig.capabilities`:
- `git`, `github` — работа с репозиториями
- `python`, `node` — runtime environments
- `docker` — Docker-in-Docker через Sysbox

### Session Management
Redis-based sessions с `--resume session_id` для сохранения контекста между сообщениями.

## Сервисы

| Сервис | Описание |
|--------|----------|
| `api` | FastAPI + SQLAlchemy — проекты, серверы, users, configs |
| `telegram_bot` | Telegram интерфейс → workers-spawner |
| `workers-spawner` | Docker контейнеры с CLI агентами |
| `langgraph` | Engineering/DevOps subgraphs |
| `scheduler` | Background tasks (sync, health checks) |
| `preparer` | Copier runner для scaffolding |
| `infrastructure-worker` | Ansible runner, SSH операции |

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
| `infrastructure-worker` | Ansible runner для деплоя |

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
