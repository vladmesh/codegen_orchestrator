# Agents Playbook

Instructions for AI assistants working on this project.

## Navigation

| Document | When to read it |
|----------|-------------|
| [docs/DEV_PIPELINE.md](docs/DEV_PIPELINE.md) | **MANDATORY READ** — the feature lifecycle and the data-driven process |
| [docs/STATUS.md](docs/STATUS.md) | **Always first** — the current task and context |
| [docs/backlog.md](docs/backlog.md) | The deferred pool of tasks and ideas (maintained by hand) |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | Before changing DTOs, queues, the API |
| [ARCHITECTURE.md](ARCHITECTURE.md) | To understand the system as a whole |
| [docs/NODES.md](docs/NODES.md) | A description of the LangGraph agent nodes |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phases and milestones |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | What has already been done |

## Related projects

- **service-template** (`/home/vlad/projects/service-template`) — a framework for generating projects

## Dev Pipeline

Tasks for developing the orchestrator itself are created and tracked in an external pipeline, not in the local Tasks DB. The local process is sprint-based, its state lives in markdown files that are maintained by hand (there are no generators for them). Full description: [docs/DEV_PIPELINE.md](docs/DEV_PIPELINE.md).

```
/go (dispatcher — reads docs/STATUS.md, first match wins)
 ├─ no sprint ─────────────── /new-sprint
 ├─ phase without tasks ───── /plan-phase
 ├─ tasks exist ───────────── /implement (one task at a time, TDD)
 ├─ all tasks done ────────── /close-phase
 └─ all phases done ───────── endgame: /audit + /e2e-run → fixes → /update-docs → /close-sprint
```

Skills take their context from `docs/STATUS.md` (the current sprint and phase) and work with the sprint markdown files in `docs/sprints/NNN-slug/` directly.

**Code outside the flow** is allowed for small fixes (< 3 files). Required: a CHANGELOG entry + a commit with the `[hotfix]` prefix. Larger changes go through the flow only.

## TDD Workflow (MANDATORY)

Red → Green → Refactor. No exceptions.

1. **Context**: read `docs/STATUS.md` and `docs/CONTRACTS.md`
2. **Red**: write a test in `services/<service>/tests/{unit,integration}/`, make sure it fails
3. **Green**: the minimal code to make the test pass
4. **Gate**: `make test-unit` + `make lint`. Update STATUS, CHANGELOG, backlog by hand as needed.

**Review Trigger**: a change to `shared/contracts/` or the DB schema that is not described in the plan → **STOP**, ask the user.

## Rules

**Documentation language** — project documentation is written in English, including new entries in `docs/CHANGELOG.md` and `docs/STATUS.md`.

**Environment variables** — never use default values:
```python
# Wrong
api_key = os.getenv("OPENAI_API_KEY", "sk-test")

# Correct
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not set")
```

**Logging** — `structlog` everywhere, never `print()`:
```python
from shared.log_config import setup_logging
import structlog
setup_logging(service_name="my_service")
logger = structlog.get_logger()
```

**LangGraph nodes** — state as a TypedDict, return a dict:
```python
async def my_node(state: OrchestratorState) -> dict:
    return {"messages": [...], "current_agent": "my_node"}
```

## Makefile

```bash
make up / down / build       # Docker lifecycle
make migrate                 # Run DB migrations
make lint                    # Ruff linter
make format                  # Ruff formatter
make test-unit               # Unit tests (fast, no deps)
make test-integration        # Integration tests (require DB/Redis)
make test-service SERVICE=api # Per-service integration test
```

## Skills (`.claude/skills/`)

Some skills have alternatives for non-Claude agents in `.agents/workflows/`.

| Skill | Description |
|-------|----------|
| `/go` | Dispatcher: reads `docs/STATUS.md`, invokes the right skill |
| `/new-sprint` | Create a sprint from VISION + ROADMAP + backlog |
| `/plan-phase` | Generate the task files for the current phase (with an architecture gate) |
| `/implement` | A TDD cycle for one sprint task, PR + CI + merge |
| `/close-phase` | Integration tests + moving to the next phase |
| `/close-sprint` | The final gate: push, CHANGELOG, ROADMAP, STATUS history |
| `/audit` | A code scan + a check of the VISION invariants; findings → `docs/backlog.md` |
| `/e2e-run <test> [--with-po] [--no-cleanup] [--feature]` | An E2E test (engineering → CI → deploy → verify, `--feature` skips scaffolding) |
| `/test-maintenance` | Running/fixing integration tests locally |
| `/brainstorm <topic>` | A structured discussion of a topic → `docs/brainstorms/<topic>.md` |
| `/update-docs` | Synchronizing the living documentation with the code |
| `/optimize` | Processing skill feedback (`docs/skill-feedback.md`) and auto-improvement |
| `/architect` | Decomposing a Story → Tasks (for client projects, through the API) |
| `/escort` | Escorting a real user through the full pipeline |
