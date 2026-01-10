# Architecture Documentation

Документация по архитектуре codegen_orchestrator.

## Documents

| Документ | Описание |
|----------|----------|
| [GLOSSARY.md](./GLOSSARY.md) | Словарь терминов. Единая терминология для проекта. |
| [CONTRACTS.md](./CONTRACTS.md) | Контракты очередей. Pydantic схемы для всех Redis queues. |
| [MIGRATION_PLAN.md](./MIGRATION_PLAN.md) | План миграции. Поэтапный план приведения к целевой архитектуре. |

## Quick Reference

### Terminology

| Термин | Значение |
|--------|----------|
| **Service** | Долгоживущий процесс (api, telegram-bot) |
| **Consumer** | Сервис слушающий Redis queue |
| **Worker** | Эфемерный контейнер с CLI-Agent |
| **CLI-Agent** | AI внутри Worker (Claude Code, Factory.ai) |
| **Task** | Сущность в БД, отслеживает работу |
| **Message** | Данные в Redis queue |
| **Event** | Уведомление о прогрессе |
| **Node** | Узел LangGraph графа |

### Queues

| Очередь | Producer | Consumer |
|---------|----------|----------|
| `engineering:queue` | CLI, Telegram | engineering-consumer |
| `deploy:queue` | CLI, Telegram | deploy-consumer |
| `scaffolder:queue` | LangGraph | scaffolder |
| `provisioner:queue` | Scheduler | infra-consumer |
| `ansible:deploy:queue` | DevOps Subgraph | infra-consumer |
| `worker:commands` | LangGraph | worker-manager |
| `worker:responses` | worker-manager | LangGraph |

### Services

```
┌─────────────────────────────────────────────────────────┐
│                      SERVICES                           │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  api                  FastAPI, source of truth (CRUD)   │
│  telegram-bot         User interface + PO sessions      │
│  langgraph            Orchestration (engineering/deploy)│
│  scheduler            Background tasks (sync, health)   │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                      CONSUMERS                          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  langgraph            engineering:queue, deploy:queue   │
│  infra-service        provisioner:queue, ansible:deploy │
│  scaffolder           scaffolder:queue                  │
│  worker-manager       worker:commands                   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## MVP: Product Owner Flow

В новой архитектуре мы уходим от прямой оркестрации через LangGraph для верхнеуровневых задач.

**Ключевая концепция:**
- **Product Owner (PO)** — это Worker (AI Agent), который является единой точкой входа для User.
- Пользователь общается с PO.
- PO использует CLI для управления проектами и процессами.
- PO не знает деталей реализации Engineering/Deploy, для него это "черные ящики" (отделы).

### High-Level Flow

```mermaid
graph TD
    User((User)) <--> |Telegram| Bot[Telegram Bot Service]
    Bot <--> |Command Queue| PO[Product Owner Worker]
    
    subgraph "Worker Container"
        PO -- "Claude/Factory" --> CLI[Orchestrator CLI]
    end
    
    CLI --> |API Request| API[API Service]
    
    API --> |"push task"| EngQueue[engineering:queue]
    API --> |"push task"| DeployQueue[deploy:queue]
    API --> |"data"| DB[(PostgreSQL)]
    
    EngQueue --> EngConsumer[Engineering Consumer]
    EngConsumer --> EngGraph[Engineering Subgraph]
    
    DeployQueue --> DepConsumer[Deploy Consumer]
    DepConsumer --> DepGraph[DevOps Subgraph]

    %% Feedback Loops
    EngGraph --> |"Result / Progress"| DB
    DepGraph --> |"Result / Progress"| DB
```

### Roles & Responsibilities

1.  **Product Owner (Worker)**:
    *   **Agent**: Настраиваемый (Claude Code, Factory.ai). Промпт в `CLAUDE.md` / `AGENTS.md`.
    *   **Задача**: Понять желание пользователя -> Транслировать в CLI команды.
    *   **Пример**: User: "Сделай блог". PO: `orchestrator project create --name blog ...` -> `orchestrator engineering start ...`.

2.  **Engineering Subgraph (The "Engineering Dept")**:
    *   **Input**: ТЗ и репозиторий.
    *   **Process**: Scaffolder -> Developer -> Tester.
    *   **Output**: Готовый PR/Commit.
    *   **PO View**: "Я отдал задачу инженерам, жду результат".

3.  **DevOps Subgraph (The "Ops Dept")**:
    *   **Input**: Проект и сервер.
    *   **Process**: EnvAnalyzer -> Deployer -> Ansible.
    *   **Output**: Работающий URL.
    *   **PO View**: "Я сказал админам выкатить, они сказали ок".

4.  **Schedulers**:
    *   Работают независимо.
    *   Sync repos, check servers health, update stats.
    *   Поддерживают "картину мира" в актуальном состоянии для PO.
