# Agents Playbook

Canonical instructions for every AI assistant working on this project, including Claude Code.
`CLAUDE.md` is a compatibility entrypoint that directs Claude Code here; do not duplicate rules
between the two files.

## Navigation

| Document | When to read it |
|----------|-------------|
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | Before changing DTOs, queues, the API |
| [ARCHITECTURE.md](ARCHITECTURE.md) | To understand the system as a whole |
| [docs/PIPELINE_V2.md](docs/PIPELINE_V2.md) | The generation pipeline stage by stage |
| [docs/NODES.md](docs/NODES.md) | A description of the LangGraph agent nodes |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | What an entity is called and what it means |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | What has already been done |
| [docs/coding-agents.md](docs/coding-agents.md) | Coding-agent integration and instruction injection |
| [docs/TESTING.md](docs/TESTING.md) | Test layers, commands, and CI coverage |
| [docs/SECRETS.md](docs/SECRETS.md) | Secret isolation and operational handling |

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

## Project conventions

**Fail fast** — this is a prototype, not legacy software. Do not add fallback values, compatibility
shims, or speculative branches that hide a missing required value. Required environment variables
raise a clear error; required mappings and typed objects are accessed directly.

**Contracts first** — statuses, queue names, and messages use the types in `shared/`; do not
construct ad-hoc dict payloads or invent string values. Check `docs/CONTRACTS.md` before changing
DTOs, queues, or API boundaries.

**Terminology** — use the terms defined in `docs/GLOSSARY.md`. In particular, a Worker is an
ephemeral coding-agent container, a Consumer is a queue role, a Service is a long-lived process,
and a Service Agent is a LangGraph agent within a service.

**Secret isolation** — do not expose real secrets to an LLM. Pass handles through agent state and
have Python resolve secret values at the execution boundary. See `docs/SECRETS.md` and
`docs/resource-management.md`.

**Test behavior at the right boundary** — prefer service tests for DB or Redis work and integration
tests for cross-service flows. Unit tests are for pure logic and fast feedback; they must verify
observable behavior rather than implementation details. Do not mock a real boundary that the
relevant test layer can exercise.

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
