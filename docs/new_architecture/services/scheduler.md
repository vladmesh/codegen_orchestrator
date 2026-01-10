# Service: Scheduler

**Service Name:** `scheduler`
**Responsibility:** Background periodic tasks (cron-like).

## 1. Philosophy

The `scheduler` maintains the "world state" in the database. It runs background tasks that keep data fresh and synchronized with external sources (GitHub, Time4VPS).

> **Rule #1:** Scheduler only reads external APIs and writes to DB via API.
> **Rule #2:** Scheduler does NOT execute business logic — it triggers other services if needed.
> **Rule #3:** All tasks are idempotent and safe to restart.

## 2. Responsibilities

1.  **Data Synchronization**: Keep DB in sync with external sources.
2.  **Health Monitoring**: Periodic checks of infrastructure.
3.  **Trigger Maintenance**: Retry failed operations, clean up stale data.

## 3. Tasks

| Task | Interval | Purpose |
|------|----------|---------|
| `github_sync` | 5 min | Sync projects from GitHub organization |
| `server_sync` | 5 min | Sync servers from Time4VPS provider |
| `health_checker` | 2 min | Monitor server health via SSH |
| `rag_summarizer` | 10 min | Summarize project documentation |
| `provisioner_trigger` | startup | Retry pending server provisioning |

### 3.1 GitHub Sync (`github_sync`)
- Fetches repos from GitHub organization
- Creates/updates Project records in DB
- Detects new repos, archives deleted ones

### 3.2 Server Sync (`server_sync`)
- Fetches VPS list from Time4VPS API
- Creates/updates Server records in DB
- Updates IP addresses, status, specs

### 3.3 Health Checker (`health_checker`)
- SSH into each server
- Checks disk space, Docker status, connectivity
- Updates `Server.health_status` in DB
- Creates Incidents if problems detected

### 3.4 RAG Summarizer (`rag_summarizer`)
- Indexes project documentation
- Updates vector embeddings for search

### 3.5 Provisioner Trigger (`provisioner_trigger`)
- Runs once at startup
- Retries servers stuck in `pending_setup` status
- Publishes to `provisioner:queue`

## 4. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                       SCHEDULER                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   main.py              Entry point, asyncio.gather()    │
│   tasks/                                                │
│     ├── github_sync.py      GitHub API → DB             │
│     ├── server_sync.py      Time4VPS API → DB           │
│     ├── health_checker.py   SSH checks → DB             │
│     ├── rag_summarizer.py   Docs → Vector DB            │
│     └── provisioner_trigger.py  Retry logic             │
│                                                         │
└───────────────────────────┬─────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
     ┌──────────┐    ┌──────────┐    ┌──────────┐
     │  GitHub  │    │ Time4VPS │    │   API    │
     │   API    │    │   API    │    │ (→ DB)   │
     └──────────┘    └──────────┘    └──────────┘
```

## 5. Dependencies

**Allowed:**
*   `asyncio`, `aiohttp`
*   `sqlalchemy`, `asyncpg` (or via API client)
*   `redis` (for publishing to queues)
*   `paramiko` / `asyncssh` (for health checks)
*   `structlog`

**External APIs:**
*   GitHub API (via PyGithub or REST)
*   Time4VPS API

## 6. Refactoring Notes

### 6.1 Minimal changes for MVP
- Current implementation works well
- Add stricter Pydantic validation for new contracts
- Ensure all DB writes go through API (not direct SQLAlchemy)

### 6.2 Future considerations
- Split into separate services if scaling needed
- Add metrics/alerting integration
