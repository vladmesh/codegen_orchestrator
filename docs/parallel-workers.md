# Parallel Workers

Isolated Docker containers with AI coding agents are used for code generation.

## Current architecture

```
┌─────────────────────────────────────────────────────┐
│                 LangGraph Orchestrator              │
│          (the Developer node in Engineering)        │
└─────────────────────────────────────────────────────┘
                         │
                  Redis streams
          (worker:commands / worker:{id}:*)
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              worker-manager Service                 │
│    (API / Docker Client / Compose Proxy)            │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│        Worker Container (ephemeral)                 │
│  - Image: worker-base-common + tooling              │
│  - Native tools: ruff, pytest, make, python         │
│  - Runs the coding task                             │
│  - Requests infrastructure through the CLI          │
└─────────────────────────────────────────────────────┘
```

## Isolation and the Flat Dev Environment

Instead of Docker-in-Docker (Sysbox), the system uses the **Flat Dev Environment** paradigm, managed from the host through `worker-manager`. This solves the problems with RAM, layer caches and stability.

1. **Dual-Network Setup**:
   Every worker is connected to two networks:
   - `internal` (the shared codegen network) — for talking to `api`, `redis` and `worker-manager`.
   - `dev_proj_<worker_id>` — an isolated network for the project's sidecar containers.

2. **Compose Proxy**:
   The workers (the injected AI agents) **have no access to Docker**. To start infrastructure dependencies they call `http://127.0.0.1:9090/infra/compose`; worker-wrapper forwards the request through the authenticated worker-broker to worker-manager.

3. **Workspace Bind-Mount**:
   The scaffolded workspace is mounted at `/workspace` inside the container. Two modes:
   - **Pre-scaffolded** (story tasks): the host path `/data/workspaces/{repo_id}/` — the repository is already prepared by the scaffolder (copier + make setup + git push), the workspace is reused between the tasks of a story.
   - **Ephemeral** (standalone tasks): `/tmp/codegen/workspaces/{worker_id}/workspace/` — created on the fly, removed after completion.
   `docker compose` on the host uses the files from that workspace to bring up the sidecar containers.

## The ban on ports, and conventions

- **No `ports:` in compose**: the template services do not publish ports on the host, because that would cause conflicts when workers run in parallel.
- The agents reach the sidecar services by host name (`db:5432`) inside the isolated `dev_proj_<worker_id>` network.

## Worker Images

The worker-base image `worker-base-common` is unified:
- Ubuntu + Python 3.12 + Node.js
- **Shared Tooling Layer**: `ruff`, `pytest`, `mypy`, `copier` and so on are installed at the image level (not duplicated per worker).
- A non-root user `worker` (uid 1000). Code on the host reached through the bind-mount does not become `root`-owned.
- Tests and linters run **natively** through per-service venvs, without starting extra ephemeral containers.

## Worker Lifecycle & Cleanup

### Stream Cleanup
`delete_worker()` now cleans `worker:{id}:input` and `worker:{id}:output` streams (were orphaned forever before).

### Orphan GC
Reverse check (Redis → Docker): scans `worker:status` entries and cleans stale ones where the container is gone. Introspect API shows `GONE` status for stale workers.

### Workspace GC
Scans both `WORKSPACE_BASE_PATH` and `SCAFFOLDED_WORKSPACE_PATH`. Max age: 35h. Also cleans stale `workspace:active_projects` Redis entries. When workspace is deleted, calls `POST /repositories/{repo_id}/notify-workspace-deleted` to clear `workspace_ready` flag so scaffolder re-creates it before next task dispatch.

### Stale Worker Auto-Cleanup
`_check_project_lock()` verifies `worker:status` — workers in terminal states (DEAD/FAILED/STOPPED) get their Redis keys cleaned up automatically, unblocking new task dispatch without manual intervention.

## Worker-Manager Introspection API

`/api/introspect/` router with 7 endpoints:
- List workers, worker detail (with container info from Docker)
- Container logs (tail param, max 5000 lines)
- Workspace file tree, file content (path traversal protection)
- Prompts (CLAUDE.md + TASK.md)
- Kill worker

Admin-frontend nginx proxies `/wm-api/` → worker-manager.
