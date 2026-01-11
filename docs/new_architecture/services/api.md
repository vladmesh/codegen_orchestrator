# Service: API

**Service Name:** `api`
**Responsibility:** Database Access Layer (CRUD).

## 1. Philosophy: The "Thin" Layer

The `api` service is the **single source of truth** for persistent data. It provides REST endpoints for all CRUD operations but does **NOT** contain any business logic.

> **Rule #1:** API never triggers external processes (no Redis publish, no GitHub calls, no scaffolding).
> **Rule #2:** API only reads from DB or writes to DB. Nothing else.
> **Rule #3:** All business logic lives in Consumers/LangGraph. API is a dumb data store.

## 2. Responsibilities

1.  **CRUD Operations**: Create, Read, Update, Delete for all entities.
2.  **Schema Validation**: Pydantic schemas for request/response validation.
3.  **Access Control**: Basic authorization via `X-Telegram-ID` header.
4.  **Database Migrations**: Alembic migrations for schema evolution.

## 3. Entities (Routers)

| Router | Entity | Purpose |
|--------|--------|---------|
| `/api/projects` | Project | Project registry |
| `/api/tasks` | Task | Task lifecycle tracking |
| `/api/servers` | Server | VPS server inventory |
| `/api/allocations` | PortAllocation | Port assignments on servers |
| `/api/users` | User | Telegram users |
| `/api/incidents` | Incident | Server incidents |
| `/api/service_deployments` | ServiceDeployment | Deployed services |
| `/api/agent_configs` | AgentConfig | LLM agent configurations |
| `/api/cli_agent_configs` | CLIAgentConfig | CLI agent configurations |
| `/api/api_keys` | APIKey | Access keys |
| `/api/available_models` | AvailableModel | LLM model registry |
| `/api/rag` | RAG | Vector search indices |
| `/api/task_executions` | TaskExecution | Worker execution usage/results |

## 4. ORM Ownership

The `api` service is the **ONLY** service that owns ORM models.

### Location

```
api/src/models/
├── base.py           # SQLAlchemy Base
├── project.py        # Project model
├── task.py           # Task model
├── server.py         # Server model
├── user.py           # User model
├── server.py         # Server model
├── user.py           # User model
├── task_execution.py # TaskExecution model
└── ...               # All other DB entities
```

### Why API owns ORM?

1. **Single Source of Truth**: Only one service can modify DB schema
2. **No SQLAlchemy in other services**: LangGraph, Scheduler, etc. use REST API
3. **Clean migrations**: Alembic migrations live in API, no conflicts
4. **Testability**: Other services mock API calls, not DB

### Other services use DTO

```python
# LangGraph, Scheduler, Telegram — use DTOs from shared/contracts/
from shared.contracts import ProjectDTO, TaskDTO

# API internally: ORM for DB, converts to DTO for responses
from src.models import Project  # ORM
from shared.contracts import ProjectDTO  # DTO for response
```

## 5. What API Does NOT Do

Previously, `create_project` contained:
- GitHub repo creation
- Secrets injection
- Publishing to `scaffolder:queue`

**All of this is removed.** API only:
1. Validates input.
2. Creates `Project` row in DB with `status = "created"`.
3. Returns the created entity.

The **caller** (CLI, Telegram Bot, or another service) is responsible for triggering workflows:
```
CLI → POST /api/projects → creates row → returns Project
CLI → POST /api/tasks (type=engineering) → creates Task row
CLI → XADD engineering:queue → triggers Engineering Flow
```

## 6. Dependencies

**Allowed:**
*   `fastapi`, `uvicorn`
*   `sqlalchemy`, `asyncpg`
*   `alembic`
*   `pydantic`
*   `structlog`

**BANNED:**
*   `redis` (no queue publishing)
*   `github` / `PyGithub` (no external API calls)
*   `copier`, `git` (no business logic)
*   Any LangChain/LangGraph dependencies

## 7. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                          API                            │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   main.py           FastAPI app + middleware            │
│   routers/          CRUD endpoints                      │
│   schemas/          Pydantic models (HTTP layer)        │
│   dependencies.py   DI (session, auth)                  │
│   database.py       SQLAlchemy engine                   │
│   migrations/       Alembic migrations                  │
│                                                         │
└───────────────────────────┬─────────────────────────────┘
                            │
                            ▼
                    ┌───────────────┐
                    │  PostgreSQL   │
                    │   (shared)    │
                    └───────────────┘
```

## 8. Refactoring Notes

### 7.1 Remove from `routers/projects.py`:
- GitHub repo creation (`github_client.create_repo`)
- Secrets injection (`github_client.set_secrets`)
- Redis publish (`redis_client.xadd("scaffolder:queue", ...)`)

### 7.2 Extract common auth logic:
- `_resolve_user()` duplicated in every router → move to `dependencies.py`

### 7.3 Simplify schemas:
- `ProjectCreate` **must** include `modules` so Scaffolder knows what to generate.
- `ProjectCreate` **must** include `modules` so Scaffolder knows what to generate.
- API stores these modules in the `Project` entity.

### 7.4 Add `TaskExecution` Model

New model to store results from workers:

```python
# api/src/models/task_execution.py

class TaskExecution(Base):
    """
    History of worker executions for a task.
    Stores 'WorkerResult' from CONTRACTS.md.
    """
    __tablename__ = "task_executions"

    id = Column(String, primary_key=True)  # request_id from worker
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True) # Optional link to high-level task
    worker_id = Column(String, nullable=False)
    
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=False)
    duration_ms = Column(Integer, nullable=False)
    exit_code = Column(Integer, nullable=False)
    
    # The JSON payload (AgentVerdict or Error)
    result_data = Column(JSONB, nullable=True) 
    
    # Derived from result_data for easy querying
    status = Column(String, nullable=False) # success/failure/in_progress/error
    
    created_at = Column(DateTime, default=datetime.utcnow)
```

