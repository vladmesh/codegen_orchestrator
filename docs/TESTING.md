# Test Infrastructure

> **Актуально на**: 2026-03-10

## Test Layers

| Layer | Location | Dependencies | CI | Speed |
|-------|----------|-------------|-----|-------|
| **Unit** | `services/{svc}/tests/unit/`, `shared/tests/`, `packages/*/tests/unit/` | None (mocks) | Pre-push + CI | ~12s (parallel) |
| **Service** | `services/{svc}/tests/service/` | Docker (single service) | CI | ~5-10 min |
| **Integration** | `tests/integration/{backend,template,infra,frontend}/` | Docker Compose (full stack) | CI (with `run-integration-tests` label) | ~10-30 min |
| **E2E** | `.claude/skills/e2e-*` (manual), `tests/e2e/` (scripts) | Full stack + real LLM | Manual only | 10-60 min |

## Running Tests

```bash
# Unit (fast, no deps — run before every push)
make test-unit                 # All services (parallel, ~12s)

# Serial mode (verbose output per service)
uv run bash scripts/test-unit-local.sh --serial

# Service (Docker, single service)
make test-service SERVICE=api

# Integration (Docker Compose, full stack)
# NOTE: In CI, these only run if the PR has the 'run-integration-tests' label.
make test-integration          # All (auto-discovers docker/test/integration/*.yml)
make test-integration-backend  # Specific suite

# E2E
make test-e2e-scaffold         # Quick scaffold smoke test (~2-3 min)
# Full E2E — use skills:
#   /e2e-run todo_api         # Runs full E2E test for the scenario

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
| langgraph | 20+ files | 3 files (engineering, reject flow, PO tools) | 3 tests (engineering worker flow) | via /e2e-run |
| worker-manager | 15 files | — | 4 tests (worker creation/execution) | via /e2e-run |
| scheduler | 2 files | 2 files | — | — |
| telegram_bot | 3 files | — | via frontend suite | — |
| infra-service | — | — | 1 file (Ansible) | — |
| shared | 9 files | — | — | — |
| packages (cli, wrapper) | 11 files | — | 2 files | — |

## E2E Testing

E2E tests are NOT in CI — they require a running stack + real LLM calls.

**Skills** (preferred way to run E2E):
- `/e2e-run <scenario> [--with-po] [--no-cleanup] [--no-nuke] [--feature]` — 7 scenarios (todo_api, echo_bot, landing_page, weather_bot, url_shortener, bot_landing, expense_tracker)

**Reports**: Written to `docs/e2e_results/`.

**Scripts** (`tests/e2e/`): Lower-level test scripts (mock anthropic, dev env smoke, live smoke). Used for development, not production E2E.

## Integration Test Architecture

The backend integration suite (`docker/test/integration/backend.yml`) spins up the full stack:
- **Services**: api, langgraph, engineering-worker, worker-manager
- **Infra**: PostgreSQL (tmpfs), Redis, Docker-in-Docker
- **Test runner**: pytest container on the same network

**Data seeding**: Tests create data via API endpoints (`POST /api/projects/`, `/api/tasks/`, `/api/servers/`). Factory fixtures in `conftest.py` (`seed_project`, `seed_task`, `seed_server`) handle creation and cleanup.

**External boundaries**: GitHub API and LLM APIs are NOT configured in the test environment. Tests verify the flow works through real services up to the external boundary, where it fails predictably (e.g., `GITHUB_ORG` not set).

**Shared helpers**: `wait_for_stream_message`, `wait_for_create_response`, `poll_task_status` in `conftest.py` — used by both worker and langgraph tests.

## Best Practices

1. Unit tests: fast (< 1s each), mock external deps
2. Integration tests: seed data via API, use unique IDs per test, cleanup via fixtures
3. One assertion per test where practical
4. Descriptive names: `test_user_creation_fails_without_email` not `test_user_1`

## Troubleshooting

- **Import errors**: Check `PYTHONPATH` includes `src/` (unit test runner sets this via `scripts/test-unit-local.sh`)
- **DB connection errors**: `make up` first, check `docker compose ps` for healthchecks
- **Stale test containers**: `make test-clean`
