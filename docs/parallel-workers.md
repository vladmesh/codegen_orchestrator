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

## Engineering consumer slots

A coding worker is only the last step. Before it, an entry has to be taken off
`engineering:queue` by the `engineering-worker` consumer, and that consumer is
what decides how many projects can be worked on at the same time.

`engineering.worker_slots` (system config, default `1`) is how many jobs one
consumer runs at once. At `1` the consumer behaves as it always has: read one
entry, run it to its terminal outcome, read the next — so a second user's
project waits for the whole turn of the first, up to `AGENT_TURN` plus overhead.
Above `1`, the consumer keeps reading while earlier jobs run.

The value is read at start-up and re-read every 30 seconds, so it changes under
a running consumer with no redeploy and no restart. Lowering it never
interrupts a running job — the gate stops handing out slots and drains. `0`
stops new work from being taken while everything in flight finishes. The
consumer refuses more than `MAX_QUEUE_SLOTS` (4): a coding worker is capped at
4 GiB and the orchestrator host is 8 GiB, so a mistyped slot count is an OOM
that takes the API, Redis and the database with it. Raising the real limit is a
measurement, not a config edit. The seeder treats the key as operator-owned: it
is written once when absent and never overwritten by a later deploy.

### Four numbers that are not the same number

These are routinely confused, and they mean different things:

| Number | Owner | What it bounds |
|---|---|---|
| `work_admission.max_concurrent_paid_runs` | API admission | How many paid runs may *exist* queued or running |
| `engineering.worker_slots` | engineering consumer | How many of them are actually *worked on* at once |
| live-work lease | `_live_work.py` | Which project is being worked on *right now*, per project |
| provider capacity | executor diagnostics | Whether Claude or Codex can take the work at all |

A run admitted by the first number still waits for the second. Until admission
reads the effective slot count, the ceiling is a promise about existence, not
about throughput — read it that way.

### Why a reclaimed entry is not free to take

XAUTOCLAIM knows only that an entry is idle, and an engineering turn is idle for
its whole hour. Once a consumer runs several jobs at once, its own PEL sweep
starts returning entries it is working on, and a second consumer's sweep would
return entries the first is working on. Two guards stop that from becoming the
same job twice in one workspace:

- entries this consumer is running are held in an in-process registry and
  skipped on sight;
- for every other process, a reclaimed entry is checked against the durable
  per-project live-work lease. A lease still inside its window means the owner
  is alive and the entry is left alone. A crashed owner stops refreshing and its
  lease falls out of the window within `LIVE_WORK_LEASE_SECONDS`, after which the
  entry is taken over exactly once.

An unreadable lease counts as live. A delayed job is recoverable; two agents in
one workspace is not.
