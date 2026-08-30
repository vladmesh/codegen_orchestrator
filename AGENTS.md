# Agents Playbook

Instructions for AI assistants working on this project.

## Navigation

| Document | When to read it |
|----------|-------------|
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | Before changing DTOs, queues, the API |
| [ARCHITECTURE.md](ARCHITECTURE.md) | To understand the system as a whole |
| [docs/PIPELINE_V2.md](docs/PIPELINE_V2.md) | The generation pipeline stage by stage |
| [docs/NODES.md](docs/NODES.md) | A description of the LangGraph agent nodes |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | What an entity is called and what it means |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | What has already been done |

## Related projects

- **service-template** (`/home/dev/projects/service-template`) — a framework for generating projects

## Where the work comes from

Work on the orchestrator itself is scoped, sequenced and tracked outside this repository, on the
Pipeline board. A task arrives as a card with its own spec; this repository holds no sprint state,
no backlog and no dispatcher skill. Brainstorms, plans and the history of past sprints live in the
knowledge store of the installation that drives the work, under
`state/knowledge/projects/codegen-orchestrator/`.

What stays here is the product: code, contracts, and the documents that describe them.

**Code outside a card** is allowed for small fixes (< 3 files). Required: a CHANGELOG entry + a commit with the `[hotfix]` prefix.

## TDD Workflow (MANDATORY)

Red → Green → Refactor. No exceptions.

1. **Context**: read the card spec and `docs/CONTRACTS.md`
2. **Red**: write a test in `services/<service>/tests/{unit,integration}/`, make sure it fails
3. **Green**: the minimal code to make the test pass
4. **Gate**: `make test-unit` + `make lint`, plus a CHANGELOG entry.

**Broad check under Secretary (one canonical form):**
`python3 -m secretary check broad --reuse --module shared` — the same suite as `make test-unit`
(`python -m shared` runs the tree's `scripts/test-unit-local.sh`, fixture env included).
Order: focused tests while editing → this broad check once, after the last edit, on the dirty tree →
commit. The receipt is keyed by the content tree, so committing the same content keeps it: after the
commit quote `check show --module shared`, do not run the suite again. Do not wrap `make test-unit`
in `--command`, that receipt is never reusable, and do not substitute a narrower `--module pytest ...`.

**Review Trigger**: a change to `shared/contracts/` or the DB schema that is not described in the plan → **STOP**, ask the user.

## Rules

**Documentation language** — project documentation is written in English, including new entries in `docs/CHANGELOG.md`.

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

These exercise the running pipeline and maintain the repository. Scoping and sequencing skills are
not here: that happens on the Pipeline board.

| Skill | Description |
|-------|----------|
| `/e2e-run <test> [--with-po] [--no-cleanup] [--feature]` | An E2E test (engineering → CI → deploy → verify, `--feature` skips scaffolding) |
| `/escort` | Escorting a real user through the full pipeline |
| `/architect` | Decomposing a Story → Tasks (for client projects, through the API) |
| `/test-maintenance` | Running/fixing integration tests locally |
| `/update-docs` | Synchronizing the living documentation with the code |
