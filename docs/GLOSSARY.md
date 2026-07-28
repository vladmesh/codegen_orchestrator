# Glossary

Single source of terminology for the codegen_orchestrator project.

## Core Concepts

### Service
A long-lived process. One container = one service.

**Examples:** `api`, `telegram-bot`, `langgraph`, `scheduler`

### Consumer
**A role, not a service name.** Any service or component that listens to a Redis queue.

A service becomes a consumer only in the context of a specific queue:
- `langgraph` — consumer of `engineering:queue`, `deploy:queue`
- `infra-service` — consumer of `provisioner:queue` (provisioning) and `env-observation:queue` (reading a deployed service's environment back)
- `worker-manager` — consumer of `worker:commands`
- `worker-wrapper` — consumer of `worker:*:input` (inside the worker container)

> **Important:** Do not confuse this with a service name. There is no `engineering-consumer` service — there is the `langgraph` service, which is a consumer of the `engineering:queue` queue.

### Worker
A Docker container with a CLI coding agent inside. Used only for Developer workers.

| Type | Lifecycle | Queue Pattern | Session |
|------|-----------|---------------|---------|
| **Developer Worker** | Per-story (reused) or per-task (standalone) | `worker:{worker_id}:*` | No (stateless) |

**Developer Worker** — a container with a coding agent. For tasks inside a Story it is reused between tasks (worker_id is stored in the Redis hash `story:workers`). For standalone tasks it is ephemeral and removed after completion. Stateless — its context is the code in the repo plus the errors.

**Managed by:** `worker-manager`
**Configuration:** prompts are stored in `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`. Worker-manager maps them to agent-specific files through `get_instruction_path()`: Claude → `CLAUDE.md`, Factory and Codex → `AGENTS.md`. A `TASK.md` with the specific task is injected as well.

### Project Status
The project lifecycle. Minimal set: `draft` → `active` → `paused` / `archived`. It carries no process statuses (scaffolding, deploying) — activity is determined by child entities (Story, Run).

### Application
A runtime entity that links a repository to a server. One application = one deployable unit on a specific server. Unique by the pair `(repo_id, server_handle)`. Tracks runtime status through `ApplicationStatus`.
**Statuses:** `not_deployed`, `deploying`, `running`, `stopping`, `stopped`, `undeploying`, `down`, `degraded`
**Relations:** Repository (repo_id), Server (server_handle)
**Table:** `applications`

### Deployment
An immutable record of a deploy attempt. Every deploy creates a new record. Linked to an Application through `application_id`. The result is recorded through `DeploymentResult`: `pending`, `success`, `failed`, `canceled`.
**Table:** `service_deployments`

### Service Status
The runtime state of a deployed project service. Separate from the project lifecycle status. Values: `not_deployed`, `running`, `degraded`, `down`, `stopped`. Stored in `project.service_status`.

### Repository Status
Availability of the git repository. Values: `active` (available on GitHub), `missing` (deleted or unavailable). Replaces the old `ProjectStatus.MISSING`.

### Service Agent
A LangGraph ReactAgent living inside the langgraph service, doing specialized domain work with access to the consumer's tools.
Unlike a CLI agent, it does not use an isolated Docker container and is part of a long-lived process.

**Examples:** Product Owner, Architect (located in `services/langgraph/src/agents/`)

### Product Owner (PO)
A service agent in the langgraph service (`services/langgraph/src/agents/po/`).
- Receives messages through `po:input`, replies through `po:response:{request_id}`.
- Uses Python @tool functions to call the API and Redis.
- PostgreSQL checkpointer to preserve context between messages (per-user thread).
- Delegates tasks to "departments" (Engineering, DevOps).

### Architect
A service agent in the langgraph service (`services/langgraph/src/agents/architect/`).
- A one-shot agent listening to `architect:queue`.
- Analyzes features (Stories) from the database and decomposes them into concrete development tasks (Tasks).

### CLI-Agent
The AI that works inside a Developer Worker container.
**Implementations:** Claude Code, Factory.ai Droid, OpenAI Codex CLI.

**Difference from a Service Agent:** a CLI-Agent is a "personality" in an ephemeral container with access to bash and the filesystem, while a Service Agent is a node in the graph of the langgraph service, communicating through @tool.

### Engineering vs Developer

**Engineering** — a LangGraph subgraph, the abstraction of a "development department".
**Developer** — a concrete node inside the Engineering Subgraph.

| Term | Level | Visibility | Description |
|--------|---------|-----------|----------|
| **Engineering** | Subgraph | External (PO) | An abstraction. The PO assigns a task to the "department" without knowing its internal structure |
| **Developer** | Node/Worker | Internal | A concrete implementation — the worker that writes the code |

**Rule:** the term "Developer" is used only when discussing the implementation inside the subgraph or the worker configuration.

---

## Planning & Management

### Story
A large feature or user need. Generates one or more Tasks. Lives at the level of the whole project.
**Types:** `product` (user value) | `technical` (internal initiatives, e.g. Rust migration).
**Statuses:** `created` → `in_progress` → `pr_review` → `deploying` → `testing` → `completed` (also: `reopened`, `waiting_human_review`, `failed`, `archived`). `pr_review` — all tasks are done, a PR is created from the story branch into main, waiting for CI + auto-merge. `deploying` — deploy gate: the story waits for a successful deploy. `testing` — the deployed service goes through QA testing. `waiting_human_review` — the developer agent reported a blocker; waiting for admin intervention. `reopened` — the user reported a problem with a completed/failed story; the architect reviews it and creates fix tasks.
**Table:** `stories`

### Epic
A grouping of Stories through `parent_story_id` for very large features.

### Repository
An entity in the DB that links code to a specific git repository. Every repository has a `provider_repo_id` (GitHub ID) and a `git_url`.
**Table:** `repositories`

### Task
*(Formerly WorkItem)*. The unit of work planning for a developer/agent.
**Statuses:** `backlog` → `todo` → `in_dev` → `in_ci` → `testing` → `done` (also: `blocked`, `waiting_resources`, `waiting_human_review`, `failed`, `cancelled`)
`waiting_resources` — the allocator found no current capacity to place the task, but the request fits on at least one managed server. The scheduler checks fresh metrics and automatically returns the task to `todo` without incrementing `current_iteration`; a waiting timeout moves it to `waiting_human_review`.
`waiting_human_review` — the developer agent reported a blocker through `POST localhost:9090/result` with `{"success": false, "reason": "..."}`. The pipeline is paused until admin intervention (`POST /tasks/{id}/resume`).
**Relations:** Story (optional), Repository (NOT NULL), Project.
**Table:** `tasks`

### Brainstorm
A record in the DB for discussing technical decisions before coding starts.
**Table:** `brainstorms`

## Data & Messaging

### Run
*(Formerly Task)*. An entity in PostgreSQL. Tracks the execution of asynchronous work (engineering, deploy, QA).
**Types:** `RunType` — `ENGINEERING`, `DEPLOY`, `QA`
**Statuses:** `QUEUED` → `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED`

**Relations:** Project, Story (optional, via `story_id` FK)

**Table:** `tasks` (renaming in progress)

### Message
Data in a Redis Stream queue. Contains a `task_id` and the parameters for processing.

**Do not confuse with:** Event (a progress notification)

**Format:** JSON wrapped in `{"data": "..."}`

### Event
A notification about the progress of a Run. Published to `callback_stream`.

**Types:** `started`, `progress`, `completed`, `failed`

**Used for:** showing progress to the user in Telegram

---

## LangGraph

### Node
A node of a LangGraph graph. A function or class that processes State.

**Types:**
- Functional node — a plain async function
- LLM node — uses an LLM to make decisions
- Tool executor — performs Tool calls

**Examples:** `DeveloperNode`, `DeployerNode`, `TesterNode`

### Subgraph
A group of related Nodes that carry out a certain stage of work.

**Examples:**
- Engineering Subgraph — Developer → Tester
- DevOps Subgraph — EnvironmentContractLoader → SecretResolver → ReadinessCheck → Deployer (triggers GitHub Actions)

### Tool
A function available to the LLM to call. The `@tool` decorator.

**Examples:** `create_github_repo`, `allocate_port`, `get_server_info`

---

## Background Processing

### Background Task
A periodic task in the scheduler. Cron-like execution.

**Examples:**
- `github_sync` — repository synchronization
- `server_sync` — server status synchronization
- `health_checker` — server health checks

---

## Queues

### Job Queue
A Redis Stream for asynchronous processing. A consumer reads Messages from the queue.

**Queues:**
- `engineering:queue` — development tasks
- `deploy:queue` — deploy tasks
- `provisioner:queue` — server provisioning
- `env-observation:queue` — reading a deployed service's environment back


### Command Queue
A Redis Stream for managing Workers.

**Queues:**
- `worker:commands` — commands for worker-manager (create, delete)
- `worker:responses:developer` — responses from worker-manager for Developer workers

### Story Worker Registry
The Redis hash `story:workers` — a `story_id → worker_id` mapping. The engineering consumer writes to it after the first spawn and reads it for subsequent tasks in the story. The scheduler clears it when the story completes or fails.

### Callback Stream
A Redis Stream for the progress Events of a specific Run.

**Name format:** `task_progress:{task_id}` (still uses the task prefix)

---

## Visual Summary

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   User  ──────►  Telegram Bot (Service)                             │
│                        │                                             │
│                        ▼                                             │
│                  API (Service)  ◄────►  PostgreSQL                  │
│                        │                                             │
│                        ▼                                             │
│              ┌─── Run (DB entity) ────┐                             │
│              │                        │                              │
│              ▼                        ▼                              │
│    ┌──────────────────┐    ┌──────────────────┐                     │
│    │ engineering:queue │    │   deploy:queue   │                     │
│    └────────┬─────────┘    └────────┬─────────┘                     │
│             │                       │                                │
│             ▼                       ▼                                │
│    ┌──────────────────┐    ┌──────────────────┐                     │
│    │   Engineering    │    │     DevOps       │                     │
│    │    Subgraph      │    │    Subgraph      │                     │
│    │   (LangGraph)    │    │   (LangGraph)    │                     │
│    └────────┬─────────┘    └────────┬─────────┘                     │
│             │                       │                                │
│             ▼                       ▼                                │
│    ┌──────────────────┐    ┌──────────────────┐                     │
│    │  Worker Manager  │    │  GitHub Actions  │                     │
│    │  (containers)    │    │  (deploy.yml)    │                     │
│    └────────┬─────────┘    └─────────────────-┘                     │
│             │                                                        │
│    ┌────────┼────────────┐                                          │
│    ▼        ▼            ▼                                           │
│ ┌────────┐ ┌────────┐ ┌────────┐                                    │
│ │ Worker │ │ Worker │ │ Worker │  (ephemeral, Developer only)       │
│ │(Claude)│ │(Claude)│ │(Droid) │                                    │
│ └────────┘ └────────┘ └────────┘                                    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```
