# Test Infrastructure

> **Актуально на**: 2026-03-03

## Test Layers

| Layer | Location | Dependencies | CI | Speed |
|-------|----------|-------------|-----|-------|
| **Unit** | `services/{svc}/tests/unit/`, `shared/tests/`, `packages/*/tests/unit/` | None (mocks) | Pre-push + CI | ~5 min total |
| **Service** | `services/{svc}/tests/service/` | Docker (single service) | CI | ~5-10 min |
| **Integration** | `tests/integration/{backend,template,infra,frontend}/` | Docker Compose (full stack) | CI | ~10-30 min |
| **E2E** | `.claude/skills/e2e-*` (manual), `tests/e2e/` (scripts) | Full stack + real LLM | Manual only | 10-60 min |

## Running Tests

```bash
# Unit (fast, no deps — run before every push)
make test-unit                 # All services
make test-api-unit             # Per-service
make test-langgraph-unit
make test-scheduler-unit
make test-telegram-unit

# Service (Docker, single service)
make test-service SERVICE=api

# Integration (Docker Compose, full stack)
make test-integration          # All (auto-discovers docker/test/integration/*.yml)
make test-integration-backend  # Specific suite

# E2E
make test-e2e-scaffold         # Quick scaffold smoke test (~2-3 min)
# Full E2E — use skills:
#   /e2e-run todo_api C       # Level A (code) / B (+CI) / C (+deploy)
#   /e2e-check                # Pre-flight check
#   /e2e-cleanup todo_api     # Cleanup resources

# Cleanup
make test-clean                # Remove all test containers/volumes
```

## Pre-push Hook

Runs automatically before `git push`:
1. `make lint` — ruff check
2. `make test-unit` — all unit tests

Both must pass.

## Test Coverage by Service

| Service | Unit | Service | Integration | E2E |
|---------|------|---------|-------------|-----|
| api | 6 files | 2 files | via backend suite | via /e2e-run |
| langgraph | 20+ files | — | skeleton (TODO) | via /e2e-run |
| worker-manager | 15 files | — | 4 tests (worker creation/execution) | via /e2e-run |
| scheduler | 2 files | 2 files | — | — |
| telegram_bot | 3 files | — | via frontend suite | — |
| infra-service | — | — | 1 file (Ansible) | — |
| shared | 9 files | — | — | — |
| packages (cli, wrapper) | 11 files | — | 2 files | — |

## E2E Testing

E2E tests are NOT in CI — they require a running stack + real LLM calls.

**Skills** (preferred way to run E2E):
- `/e2e-run <scenario> <level>` — 7 scenarios (todo_api, echo_bot, landing_page, weather_bot, url_shortener, bot_landing, expense_tracker), 3 levels (A/B/C)
- `/e2e-check` — pre-flight: Docker services, API health, Redis
- `/e2e-cleanup <scenario>` — remove GitHub repo, containers, DB records

**Reports**: Written to `docs/e2e_results/`.

**Scripts** (`tests/e2e/`): Lower-level test scripts (mock anthropic, dev env smoke, live smoke). Used for development, not production E2E.

## Best Practices

1. Unit tests: fast (< 1s each), mock external deps
2. Integration tests: cleanup after themselves (conftest fixtures handle this)
3. One assertion per test where practical
4. Descriptive names: `test_user_creation_fails_without_email` not `test_user_1`

## Troubleshooting

- **Import errors**: Check `PYTHONPATH` includes `src/` (unit test runner sets this via `scripts/test-unit-local.sh`)
- **DB connection errors**: `make up` first, check `docker compose ps` for healthchecks
- **Stale test containers**: `make test-clean`
