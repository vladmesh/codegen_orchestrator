# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-agent orchestrator using LangGraph for automated code generation and deployment. Input: project description via Telegram. Output: deployed project with CI/CD, domain, SSL.

**Philosophy**: Autonomous operation (human checks in periodically), agents as graph nodes, non-linear agent calls, spec-first code generation.

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
make shell                 # Open shell in tooling container
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
```

## Architecture

```
Telegram Bot → worker-manager → CLI Agent (Product Owner)
                        │
        ┌───────────────┴────────────────┐
        ▼                                ▼
    Engineering Subgraph          DevOps Subgraph
    Scaffolder → Developer → Tester     EnvAnalyzer → SecretResolver → ReadinessCheck → Deployer
```

**Key Components:**
- **CLI Agent**: Pluggable Product Owner (Claude Code, Factory.ai, custom) via worker-manager
- **Tool System**: All API endpoints exposed via OpenAPI, native tool calling
- **Session Management**: Redis-based locks (PROCESSING/AWAITING states)

**Services** (in `services/`):
- `api`: FastAPI + SQLAlchemy, stores projects/servers/agent_configs (port 8000)
- `langgraph`: LangGraph orchestration (Engineering, DevOps subgraphs)
- `engineering-worker`: Consumes `engineering:queue`, runs Engineering subgraph
- `deploy-worker`: Consumes `deploy:queue`, runs DevOps subgraph
- `telegram_bot`: python-telegram-bot interface + PO session management
- `worker-manager`: Docker container lifecycle for CLI agents (replaces `workers-spawner`)
- `scaffolder`: Runs copier for project scaffolding (async, before developer work)
- `infra-service`: Ansible execution for provisioning (consumes `provisioner:queue`)
- `scheduler`: Background workers (github_sync, server_sync, health_checker)

**Packages** (`packages/`): `orchestrator-cli` (CLI tools for agents), `worker-wrapper` (agent container entrypoint).

**Shared** (`shared/`): Logging setup (structlog), contracts (DTOs, queue schemas), models, configuration.

**External Coding Agents**: Claude Code and Factory.ai Droid for actual code generation (not custom agents).

**Related Projects**:
- `/home/vlad/projects/service-template` - Spec-first framework for generating microservices
- `/home/vlad/projects/prod_infra` - Ansible playbooks for server infrastructure

## Code Patterns

### Environment Variables
Never use default values:
```python
# Wrong
api_key = os.getenv("OPENAI_API_KEY", "sk-test")

# Correct
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not set")
```

## Important Rules

1. **TDD Workflow**: Follow Red → Green → Refactor. Write tests first.
2. **Never use default values for env vars**: Fail fast with `RuntimeError` if missing.
3. **Review Trigger**: If a change requires modifying `shared/contracts/` or DB schema not described in the plan — STOP and ask.
4. **Structured logging**: Use `structlog` everywhere, never `print()`.
5. **Run tests before committing**: `make test-unit` at minimum.

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
    server = secret_storage.get_server(server_handle)  # Python reads secret
    subprocess.run(["ansible-playbook", ...], env={"SSH_KEY": server.ssh_key})
```

## Adding New Agents

1. Create file in `services/langgraph/src/nodes/<name>.py`
2. Use `LLMNode` base class for agentic nodes or plain async function for functional nodes
3. Add node to graph in `services/langgraph/src/graph.py`
4. Define edges and routing logic
5. If needs tools: add to `services/langgraph/src/tools/`
6. If needs capability: add to `services/langgraph/src/capabilities/__init__.py`
7. Document in `docs/NODES.md`
8. Add tests in `services/langgraph/tests/unit/`

## Key Configuration

- **Ruff**: Line length 100, Python 3.12, checks: E, F, I (isort), UP, B, C4, S, PLR, C901
- **Git Hooks**: Pre-commit auto-formats (never blocks), pre-push runs lint+tests (blocks on failure)
- **Tests**: pytest with asyncio, unit tests in `tests/unit/`, integration in `tests/integration/`
- **LangSmith**: Set `LANGCHAIN_TRACING_V2=true` for agent execution tracing

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.12+ |
| Orchestration | LangGraph |
| API | FastAPI + SQLAlchemy 2.0+ |
| Database | PostgreSQL |
| Cache/Queues | Redis (streams, pub/sub) |
| Bot | python-telegram-bot |
| Logging | structlog (JSON in prod, console in dev) |
| Linting | Ruff |
| Container Isolation | Sysbox runtime |
| Secrets | project.config.secrets (PostgreSQL), GitHub Repository Secrets |

