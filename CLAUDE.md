# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-agent orchestrator using LangGraph for automated code generation and deployment. Input: project description via Telegram. Output: deployed project with CI/CD, domain, SSL.

**Philosophy**: Autonomous operation (human checks in periodically), agents as graph nodes, non-linear agent calls, spec-first code generation.

> [!IMPORTANT]
> **Where the work comes from**: tasks for the orchestrator itself are scoped and tracked outside this repository, on the Pipeline board — not in the local Tasks DB and not in markdown here. This repository holds no sprint state, backlog or roadmap bookkeeping; brainstorms, plans and past sprints live in the driving installation's knowledge store under `state/knowledge/projects/codegen-orchestrator/`. Pipeline-testing skills (`/e2e-run`, `/escort`, `/architect`) require `make up`.

## Commands

```bash
# Development
make up                    # Start all services
make down                  # Stop services
make build                 # Build all Docker images
make migrate               # Run database migrations
make makemigrations MSG='description'  # Create new migration
make seed                  # Seed database with API keys
make nuke                  # Full reset (volumes, rebuild, migrate, seed)
make lock-deps             # Regenerate all requirements.lock files

# Code Quality
make lint                  # Run Ruff linter
make format                # Format with Ruff (can specify FILES=...)
make setup-hooks           # Install git hooks

# Testing
make test-unit             # All unit tests (fast, no deps)
make test-integration      # All integration tests (require DB/Redis)
make test-all              # All tests
make test-{service}-unit   # Service-specific: api, langgraph, scheduler, telegram
make test-{service}-integration
make test-clean            # Cleanup test containers

# Server access
./infra/scripts/ssh-to-server.sh <server_ip> [command...]  # SSH via API-stored key
# Example: ./infra/scripts/ssh-to-server.sh 80.209.235.229 'docker ps'
# Fetches SSH key from API, writes to tempfile, connects. No local key needed.
```

## Architecture

`User → Telegram → PO Agent → (scaffold → architect → engineer → CI → deploy → QA) → User`

Full pipeline: [docs/PIPELINE_V2.md](docs/PIPELINE_V2.md). Agent nodes: [docs/NODES.md](docs/NODES.md). Queue contracts: [docs/CONTRACTS.md](docs/CONTRACTS.md).

**Key non-obvious details:**
- `engineering-worker` and `deploy-worker` share the `langgraph` Docker image (different entrypoints)
- `shared/` is never installed as a package. Compose services bind-mount it (`./shared:/app/shared`), images `COPY` it, tests import it from the tree via `PYTHONPATH`. Editing `shared/` needs no sync and no rebuild for bind-mounted services — see [docs/REBUILD.md](docs/REBUILD.md).
- `shared/pyproject.toml` is the only pyproject under `shared/`: it declares shared's third-party deps but installs nothing, and every consumer repeats them in its own `pyproject.toml`, enforced by `shared/tests/unit/test_shared_dependency_parity.py`. `shared` has no `[tool.uv.sources]` entry — see [docs/decisions/shared-is-not-a-package.md](docs/decisions/shared-is-not-a-package.md).
- External coding agents (Claude Code, Factory.ai Droid) run inside worker containers managed by `worker-manager`

**Related Projects**: `/home/dev/projects/service-template` — spec-first framework for generating microservices

## Critical Anti-Patterns

These three mistakes cause the most debugging pain. They apply everywhere — code, skills, plans, configs.

### 1. Fail-fast, no fallbacks

This is a prototype, not legacy. No backward compatibility needed. When something is missing or wrong — crash immediately. The faster we see the error, the faster we fix it.

```python
# WRONG — hides the problem, delays debugging
value = config.get("key", "some_default")
result = response.get("data") or fallback_value
status = task.get("status", "unknown")

# RIGHT — crash, see the error, fix the root cause
value = config["key"]  # KeyError if missing — good
result = response["data"]  # KeyError if missing — good
status = task.status  # AttributeError if wrong type — good
```

**No shims, no "just in case" branches, no `or default`.** If a function can receive `None` — don't handle it silently, raise. If an env var is missing — `RuntimeError`, not a default. If a key doesn't exist — let it crash, don't `get()` with a fallback. If you're removing something — remove it completely, no compatibility wrappers.

### 2. Enums and schemas, never hardcoded strings or dicts

Every status, queue name, and message has a defined type in `shared/`. Use it. Never guess keys or construct dicts by hand.

```python
# WRONG — hardcoded strings, guessing, multi-branch "just in case"
if status in ("done", "completed", "success"):  # three guesses at one value
redis.xadd("engineering:queue", {"data": json.dumps(payload)})
task_data = {"status": "todo", "project_id": pid}  # raw dict

# RIGHT — one source of truth
if status == TaskStatus.DONE:  # or TaskStatus.DONE.value for string comparison
await redis_client.publish_message(ENGINEERING_QUEUE, EngineeringMessage(...))
task = TaskCreate(status=TaskStatus.TODO, project_id=pid)
```

If a schema or enum doesn't exist for something — create it in `shared/contracts/`. Don't work around missing types with raw dicts.

### 3. Follow the glossary — [docs/GLOSSARY.md](docs/GLOSSARY.md)

Terms have precise meanings. Misusing them causes confusion in code, logs, container names, and docs.

Key distinctions:
- **Worker** = ephemeral Docker container with CLI coding agent inside. Only Developer Workers exist. Nothing else is a "worker".
- **Consumer** = a role, not a service name. `langgraph` service is a consumer of `engineering:queue`. Don't name containers `*-worker` if they're consumers.
- **Service** = long-lived process, one container = one service.
- **Service Agent** = LangGraph ReactAgent inside the `langgraph` service (PO, Architect). Not a worker.

When naming containers, variables, queues, or writing docs — check the glossary.

## Important Rules

1. **TDD — test behavior, not implementation**: Red → Green → Refactor still applies, but tests must verify **what the code does**, not how it's structured. Test real data pipelines, real exceptions, real status transitions — not "key exists in dict" or "function returns value". See testing philosophy below.
2. **Review Trigger**: If a change requires modifying `shared/contracts/` or DB schema not described in the plan — STOP and ask.
3. **Structured logging**: Use `structlog` everywhere, never `print()`.
4. **Documentation language**: Project documentation is written in English, including new entries in `docs/CHANGELOG.md`.
5. **Run tests before committing**: `make test-unit` at minimum.
6. **Code outside a card**: Small fixes (< 3 files) are OK with `[hotfix]` commit prefix + CHANGELOG entry. Anything larger needs a card on the Pipeline board.

### Testing Philosophy

**Prefer integration/service tests over unit tests.** Unit tests are a quick pre-push sanity check, not real coverage. A feature "covered by unit tests" is an undertested feature.

**Test hierarchy** (prefer higher):
1. **Service tests** (single service + real DB/Redis) — best bang for buck, runs in CI
2. **Integration tests** (multiple services wired together) — for cross-service flows
3. **Unit tests** (everything mocked) — only for fast pre-push smoke, not a substitute for the above

**What makes a good test:**
```python
# WRONG — tests implementation details, not behavior
def test_create_task_returns_dict():
    result = create_task(data)
    assert "id" in result
    assert result["status"] == "backlog"

# RIGHT — tests behavior through the real pipeline
async def test_task_creation_and_dispatch(api_client, redis):
    # Create task via API (real DB)
    resp = await api_client.post("/api/tasks/", json={...})
    task_id = resp.json()["id"]

    # Transition it — does the state machine work?
    await api_client.post(f"/api/tasks/{task_id}/start")

    # Was a message published to the queue?
    messages = await redis.xrange("engineering:queue")
    assert any(task_id in m for m in messages)
```

**Rules:**
- If something touches DB or Redis — write a service test, not a unit test
- If something crosses service boundaries — write an integration test
- Unit tests are for pure logic only (parsers, validators, algorithms)
- Never mock what you can test for real — mocks hide bugs at boundaries

### LangGraph Nodes
Always define state as TypedDict and return complete state:
```python
from typing import TypedDict

class OrchestratorState(TypedDict):
    messages: list
    current_project: str | None
    # ...

def my_agent(state: OrchestratorState) -> OrchestratorState:
    return {"messages": [...], ...}
```

### Logging
```python
from shared.log_config import setup_logging
import structlog

setup_logging(service_name="my_service")
logger = structlog.get_logger()
logger.info("event_name", user_id=123, duration_ms=45.2)
```

### Secret Isolation
LLM never sees actual secrets. Use handles in state, Python code reads secrets directly:
```python
@tool
def deploy_to_server(server_handle: str):
    """LLM calls with handle only."""
    server = api_client.get_server(server_handle)  # Python reads secret
    github.set_repository_secrets(repo, {"DEPLOY_HOST": server.public_ip, ...})
```

## Documentation Map

### Architecture (read when working with code)
- [ARCHITECTURE.md](ARCHITECTURE.md) — services, data flows, the system as a whole
- [PIPELINE_V2.md](docs/PIPELINE_V2.md) — target 7-phase pipeline architecture
- [NODES.md](docs/NODES.md) — agent nodes, tools, Redis Streams communication
- [CONTRACTS.md](docs/CONTRACTS.md) — queue registry, DTOs, correlation IDs
- [coding-agents.md](docs/coding-agents.md) — external agent integration (Claude Code, Factory.ai)
- [parallel-workers.md](docs/parallel-workers.md) — worker containers, networks, bind-mounts

### Operations (read when deploying/debugging)
- [DEPLOY.md](docs/DEPLOY.md) — production deployment, GitHub Secrets
- [SECRETS.md](docs/SECRETS.md) — 3-level secret model (L1 platform / L2 project / L3 user)
- [resource-management.md](docs/resource-management.md) — handles vs secrets, ResourceAllocator
- [ERROR_HANDLING.md](docs/ERROR_HANDLING.md) — error categories, retry/timeout policies
- [LOGGING.md](docs/LOGGING.md) — structlog patterns, Loki/Grafana stack
- [TESTING.md](docs/TESTING.md) — test layers (unit/service/integration/live/e2e), commands
- [GLOSSARY.md](docs/GLOSSARY.md) — project terminology
- [playbooks/line2-engineering.md](docs/playbooks/line2-engineering.md) — manual test matrix

### Direction and history
- [VISION.md](docs/VISION.md) — what the product is, who it is for, architectural invariants
- [ROADMAP.md](docs/ROADMAP.md) — product arcs and what is deferred
- [CHANGELOG.md](docs/CHANGELOG.md) — release history

