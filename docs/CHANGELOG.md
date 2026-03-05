# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/). Группировка по датам.

## 2026-03-05

### Removed
- Dead CLI agent config infrastructure (#36): `CLIAgentNode`, `cli_agent_config_cache`, CLI agent config API router/schema/ORM model, alembic migration — 423 LOC deleted

### Fixed
- Fail fast with `RuntimeError` when `ORCHESTRATOR_USER_ID` not set in CLI commands (#29) — was silently defaulting to `"unknown"`, breaking audit trail

### Changed
- Defensive init `smoke_result: None` in `_build_subgraph_input` (#25) — consistent with other Optional fields
- Diagnostic logging `devops_subgraph_result` in deploy_worker after `ainvoke()` — for #25 root cause investigation
- Updated `/e2e-run` skill to check deploy-worker logs for smoke diagnostics

### Added
- E2E report: todo_api with-PO mode PASS (12 min) — first test with PO creating project via Redis Streams
- Post-deploy smoke tester node in DevOps subgraph (#25): HTTP `/health` check for backends, Telethon `/start` check for tg_bot modules
- `SmokeTesterNode` with retry logic (3 retries, 5s delay) and graceful skip when Telethon not configured
- `smoke_result` field in `DevOpsState` — propagated through deploy_worker to task result
- Conditional routing: `deployer` → `smoke_tester` → END (skips smoke on deploy failure)
- Telethon dependency + env vars in deploy-worker compose config
- Updated `/e2e-run` skill to report smoke results

### Changed
- Extract `infra_client.py` (279 LOC) from langgraph + infra-service to `shared/clients/` (#23)
- Merge duplicated constants (`Paths`, `Timeouts`, `CI`, `Provisioning`) into `shared/constants.py` (#23)
- Service-local `config/constants.py` now re-exports from shared (#23)
- Add `shared/tests/**` to ruff PLR2004/S101 per-file-ignores (#23)
- Restructure ROADMAP: split Phase 2 → 2A (pre-MVP alpha blockers) + 2B (post-alpha stability)
- Triage: 7 new tasks (#30-#35), reopened #25 as regression, reordered backlog by roadmap phases
- New brainstorm: epic decomposition — decision: Task Store in DB (Phase 3), skip intermediate file-based epics
- Triage skill: added Queue reorder step based on ROADMAP phase priorities

## 2026-03-04

### Added
- Auto-detect stale worker images: source hash label in `worker-base-common`, `check-worker-images` target in Makefile, auto-rebuild in `make build` and E2E pre-flight
  - Root cause: `POSTGRES_HOST=project-db` bug persisted 4 E2E runs because `shared/` fix was never baked into worker image ([worker audit](e2e_results/todo_api-20260304-levelC-worker.md))
- LangGraph integration tests (#6): 3 tests against real DB/Redis/API (engineering worker flow, missing project, scaffold_failed abort)
- Engineering-worker service in backend test compose
- API data seeding fixtures (`seed_project`, `seed_task`, `seed_server`) + `poll_task_status` helper
- E2E reports: todo_api Level C PASS (14 min), weather_bot Level C PASS (15 min, first multi-module test)

### Fixed
- Enforce fail-fast for env vars (#24): notifications.py uses lazy init — import safe, first call raises RuntimeError if TELEGRAM_BOT_TOKEN/API_BASE_URL missing
- Replace `print()` with `logging.warning()` in tool_registry.py (#24)
- Replace swallowed `except: pass` with `logger.debug()` in worker-manager events.py (#24)
- Add ORCHESTRATOR_USER_ID warning in CLI commands (#24)
- Alembic migrations in test API + encryption key for integration tests (#6)
- Missing `__init__.py` for relative imports in integration tests (#6)

### Changed
- Consolidated duplicated test helpers (`wait_for_stream_message`, `wait_for_create_response`) into `conftest.py`

## 2026-03-03

### Fixed
- CI gate: filter by commit SHA to prevent scaffold CI satisfying implementation gate
- BACKEND_PORT: resolve from allocated resources instead of random secret token

### Added
- Worker network isolation (#22): `codegen_worker` network, dual-homing bridge services
- E2E report: todo_api Level C — full pass, all CRUD working (14 min end-to-end)

### Changed
- Remove obsolete EXEC_MODE=native references

### Removed
- `project-db` alias workaround and `_patch_db_hostname()` (#22) — no longer needed with network isolation

## 2026-03-02

### Fixed
- Docker network overlap in compose volume test
- Phantom TaskType re-export in shared.models (multiple attempts)
- CI unit test targets — use unified `make test-unit` with uv

### Changed
- Consolidate test suites: clean up Makefile targets, fix worker-manager tests (#6)
- Move enums to contracts/dto (single source of truth)
- Cleanup migrated service tests
- Add service tests to CI

### Added
- E2E reports: todo_api Level C — deploy failed, makemigrations investigated
- Backlog #6 audit: service test details

## 2026-02-28

### Fixed
- Compose proxy: file discovery, env leak, DNS collision
- Use `infra/` compose layout in ComposeRunner

### Added
- E2E secret injection for tg_bot Level C tests
- Deploy retry: rerun failed workflow

### Changed
- Backlog #6 audit: service & e2e test broken items documented

## 2026-02-27

### Fixed
- Compose workspace path mismatch for project-id workers
- Docker login resilience + infra failure rerun in CI
- Dead worker container detection — unblock waiting consumers

### Added
- E2E Level C run reports (multiple iterations)
- E2E skill pre-flight checks

## 2026-02-26

### Fixed
- Scaffold skip due to stale project dict + 3-level fail-fast defense
- Fail-fast when GitHub repo already exists during engineering job
- WorkflowNotFoundError fail-fast and description fallback in CI gate

### Changed
- Use GitHubAppClient instead of gh CLI in e2e-run skill

## 2026-02-25

### Added
- Encrypt API keys and SSH keys at rest using Fernet (#20)

### Changed
- Unify ServerStatus enum, remove dead IncidentDTO (#15)
- Consolidate ServiceModule enum, remove dead code (#16, #17)
- Sync worker prompts with simplified service-template (#1)
- Migrate `(str, Enum)` to `StrEnum` across codebase (21 instances, 14 files)
- Remove deprecated `update_framework` command
- Remove stale ruff.toml per-file-ignores
- Deduplicate MockProcess into shared test conftest

### Added
- Refactor audit v2 report

## 2026-02-23

### Fixed
- Pin fakeredis>=2.34.1 to eliminate deprecation warnings
- Timezone=True for Task model datetime columns
- Healthcheck intervals tuned, worker-manager lock refresh

### Added
- E2E testing skills for Line 2 engineering pipeline (e2e-run, e2e-check, e2e-cleanup)

### Fixed
- Scaffold skip bug, description passthrough, CI gate 404 handling

## 2026-02-21

### Changed
- Remove stale scaffolder references
- Add audit report collection step to Line 2 playbook

### Added
- DELETE /api/projects endpoint
- Line 2 engineering playbook

### Fixed
- Remove "backend always required" constraint from module selection

## 2026-02-20

### Added
- Scaffolder removal: inline scaffold phase into worker-manager (#1 orchestrator-side)
- Orphaned resource GC for worker-manager

### Changed
- Extend e2e scaffold test with verification and cleanup
- Remove Docker-in-Docker capability, update developer prompts (dev-env phase 4)
- Add native tooling packages to worker-base-common image

## 2026-02-19

### Added
- Workspace persistence: project_id passthrough, git token refresh, PROGRESS.md, GC by age, project mutex (phases 1-5)
- Worker reuse for CI fix loop: wrapper multi-turn, spawner API, engineering reuse, gate timeout (#8)
- Dev environment: workspace bind-mount, dual-network, compose proxy (phases 1-3)

## 2026-02-17

### Added
- Redis Streams unification: 9 consumers on `RedisStreamClient.consume()` with PEL recovery, Pydantic contracts (#3+#5)

## 2026-02-15

### Added
- Deploy architecture (9 iterations): Fernet encryption, env groups, GitHub Actions deploy, webhook auto-deploy, self-hosted Docker registry + Caddy TLS
- PO ReactAgent migration: CLI subprocess → async LLM consumer with reminder polling and direct tool access
