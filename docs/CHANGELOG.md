# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/). Группировка по датам.

## 2026-03-09

### Added
- **PO tools contract tests**: 15 unit-level contract tests that import API Pydantic schemas directly and validate PO tool payloads (ProjectCreate, StoryCreate, MergeSecretsRequest). 9 service-level integration tests that call PO tools against a real API with DB, validating full roundtrip. Replaced MockServer with real API in langgraph service test compose.

### Changed
- **Update Ruff to 0.15.5**: Bumped ruff from 0.8.4 to 0.15.5 in pyproject.toml and CI. Reformatted 17 test files (parenthesized assertion style). No functional changes.
- **Remove Docker tooling, use `uv run` everywhere**: Deleted `tooling/Dockerfile`, `docker-compose.tools.yml`, `.pre-commit-config.yaml`. Rewrote `make lint`/`format`/`lock-deps` to use `uv run` directly. Git hooks now require `uv` instead of Docker. CI uses `uv sync` + lockfile ruff instead of `--with ruff==VERSION`. Single source of truth for ruff version: `pyproject.toml` + `uv.lock`.

### Refactored
- **Architect: migrate to LangGraph ReAct agent** (#36): Moved architect from scheduler plain function to langgraph service as a ReAct agent with tool use. New `architect` Docker service (same langgraph image, separate entrypoint). Created 5 architect tools (get_story, get_project_spec, get_tasks_by_story, create_task, transition_story). Added `group` parameter to base worker for custom consumer groups. Removed architect code from scheduler service. 22 new unit tests.
- **LangGraph service directory refactoring** (#35): Renamed `src/workers/` → `src/consumers/` (with `_worker` suffix dropped from files), dissolved `src/worker/` module into `src/` root, centralized PO prompts under `src/prompts/po/`. Updated Dockerfile, docker-compose, integration test config, and all imports. Pure structure change — no business logic modifications.

### Added
- **Architect node — story decomposition into tasks + task dispatcher** (#34): Full architect pipeline: story → architect:queue → LLM decomposition → N tasks with `blocked_by_task_id` chains → task dispatcher → engineering runs. Architect consumer runs in scheduler with concurrent processing (Semaphore(5)). Task dispatcher polls every 30s: dispatches unblocked todo tasks with cumulative context from sibling task events, completes stories when all tasks done (triggers deploy + PO notification). Engineering worker now updates task status alongside run status and skips per-task deploy. PO `create_story` tool publishes to architect:queue instead of engineering:queue.

## 2026-03-08

### Fixed
- **Deploy: inter-service URL uses docker service name** (#54): `BACKEND_API_URL` and similar inter-service variables now resolve to `http://backend:8000` (docker DNS) instead of `http://<external_ip>:<port>`. Added `API_URL` to `COMPUTED_EXACT` in env_analyzer. External-facing URLs (`deployed_url`, `DEPLOY_HOST`) remain unchanged.

### Refactored
- **Split engineering_worker.py** (#18): Extracted CI gate logic into `_ci_gate.py` (480 LOC) and repo setup into `_repo_setup.py` (124 LOC). Main file reduced from 1114 to 545 LOC. Pure internal refactoring — no behavior changes.

### Changed
- **Decouple shared/ from Docker builds** (task-7e9aed9c): Replaced `pip install shared` with plain `COPY shared + PYTHONPATH=/app` in all 6 service Dockerfiles and worker-base-common. Moved shared's pip deps into each service's own pyproject.toml. Narrowed `WORKER_SOURCE_HASH` to only hash worker-relevant shared submodules. Fixed `.dockerignore` to exclude nested `.venv/` dirs. Rebuild after shared/ change: ~10s (was ~5min).

### Added
- **Deploy Pre-Check** (#21): Added `action` field (create/feature/fix) to `DeployMessage` contract. Engineering worker propagates action to deploy message on auto-deploy. Webhook-triggered deploys default to action=feature. Deploy worker SSH-checks `/opt/services/<name>/` before deploying: create fails if dir exists (leftover cleanup needed), feature/fix fails if dir absent (never deployed). Added `asyncssh` dependency.

### Changed
- **Dockerfile layer caching optimization** (#21 deviation): Split shared package install into deps-first + code-only steps across all service Dockerfiles for better layer caching. Multi-stage Claude CLI install in worker-base-claude avoids re-downloading on base image changes.

### Fixed
- **compose.dev.yml ports conflict with worker containers** (task-f9aadfc1): Compose runner now injects `.codegen-ports.yml` override that clears published ports (5432, 6379) for worker projects. Workers communicate via Docker DNS on isolated networks, so published ports are unnecessary and conflicted with orchestrator's own postgres/redis.

### Added
- **Seed DB — stories, repositories, historical tasks** (task-f7cd9611):
  - Updated project status to `developing`, migrated repo URLs to `project-factory-organization`
  - Created repositories for hammurabi-game-bot and todo-api with correct `provider_repo_id`
  - Created 2 new stories: "Refactoring & code health", "Dev process automation"
  - Created technical story "Rust migration" (for future Story type field: product/technical)
  - Linked all 40+ orchestrator tasks to stories
  - Imported 11 done + 12 backlog + 5 Rust tasks from service-template backlog
  - Cleaned up 8 smoke/test tasks and 3 test stories
  - Created task for "Replace Milestone with Story type field"
  - Updated `/triage` skill: story matching on task creation, template tasks via API with repository_id

### Changed
- **Replace Milestone with Story type field** (task-6fe23f2a): Added `type` field (product/technical) to Story model, schemas, and router with filter support. Dropped `milestones` table, `milestone_id` from tasks, and all Milestone code (model, schemas, router, DTO, tests, seed script). Rewrote `generate_roadmap.py` to generate ROADMAP.md from stories grouped by type. Updated docs (DEV_PIPELINE, GLOSSARY, checkpoint, triage skills). Set Rust migration story to type=technical.
- **Project ID → UUID + schema cleanup** (task-7163e7ac): Changed `Project.id` from `String(255)` to native PostgreSQL `UUID` with auto-generation. Migrated all 13 FK `project_id` columns to `Uuid` type. Removed legacy `github_repo_id` and `repository_url` from Project model. Added `visibility` column to Repository. Migrated webhook lookup to `Repository.provider_repo_id`. Added `get_primary_repository` to API clients. Updated all DTOs, schemas, routers, workers, tests, scripts, and skills. Alembic migration handles mixed-format ID conversion (short hex, strings, existing UUIDs).

### Added
- **TaskStatus.BLOCKED + blocked_by_task_id** [hotfix]: Added `blocked` status to task state machine with `blocked_by_task_id` FK (self-referencing). Transitions: `in_dev → blocked`, `blocked → in_dev | backlog | cancelled`. `/implement` skill updated: auto-unblocks tasks when blocker is done. Migration, schemas (create/read/update), 3 new unit tests.


- **Story: priority + blocked_by fields** (task-9d288940): Added `priority` (int, default 0) and `blocked_by_story_id` (FK → stories.id) to Story model. Migration with indexes. Schemas updated (create/read/update). List endpoint gains `priority` filter and `sort` param. Validation: cannot start a story if its blocker is not completed (422). 8 new unit tests.

- **Story model + API** (wi-34761901): New `Story` entity (`id, project_id, parent_story_id, title, description, acceptance_criteria, status, created_by`). `StoryStatus` enum: `created | in_progress | completed | archived` with valid transitions. Full CRUD API at `/api/stories/` with action endpoints (`/start`, `/complete`, `/archive`). `Task.story_id` nullable FK. Self-referencing `parent_story_id` for epic-like grouping. Alembic migration. Refactored `list_tasks` to use `_TaskFilters` dependency class (PLR0913). 47 new unit tests.

### Fixed
- **Missing Project warnings spam**: `github_sync` worker now respects `GITHUB_ORG` env var instead of indiscriminately checking the first organization the GitHub App is installed in, preventing false `MISSING` alerts when installed in multiple orgs.
- **Admin notifications spam**: `notify_admins` now correctly filters out regular users based on the `is_admin` database flag instead of blasting messages to all users in the system.

- **Test Infrastructure Audit**: Fixed 10 bugs and warnings, optimized run speed.
  - Parallelized `make test-unit` execution in bash (35s → ~12s, 2.6x speedup).
  - Fixed unmocked `notify_admins` in scheduler unit tests.
  - Fixed missing `X-Telegram-ID` header in backend integration `seed_project` fixture.
  - Replaced 9 deprecated `HTTP_422_UNPROCESSABLE_ENTITY` with `_CONTENT` across API routers.
  - Disposed app DB engine in test teardown to fix 5 asyncpg `ResourceWarning` leaks.

- **Scaffold script task_description escaping** (#52): Pass `task_description` via copier `--data-file` instead of inline `--data` to prevent shell metacharacter injection (quotes, backticks, `$()`, parentheses). Base64-encode in Python, decode inside bash into YAML file. Added 9 parametrized tests for dangerous character patterns.

### Added
- **Repository model + migration** (wi-ad3b4502): New `Repository` entity (`id, project_id, name, git_url, provider_repo_id, role, is_managed`). Full CRUD API at `/api/repositories/` with `by-provider-id` lookup. `Task.repository_id` nullable FK. Alembic migration. 10 unit tests + 2 integration tests. `RepositoryRole` enum: `primary | dependency`. Documented `uv sync --reinstall-package shared` requirement in CLAUDE.md.

- **make sync — docs generation from DB** (task-94f2783f):
  - `POST /api/tasks/push` endpoint — auto-priority (`min(backlog) - 1`)
  - `source_brainstorm_id` filter on `GET /api/tasks/` for sibling lookup
  - `scripts/generate_status.py` — STATUS.md dashboard (current task, events, stats)
  - `scripts/sync_recent_artifacts.py` — plans/brainstorms window (in_dev + last 3 done)
  - `make sync` umbrella target (backlog + roadmap + status + recent-artifacts)
  - `make task TITLE="..."` CLI wrapper for quick task creation
  - Event writes in /implement: ci_fix, plan_deviation, implementation_summary
  - Event reads in /implement and /plan: resume context + sibling tasks
  - Cleaned 20+ stale plan files and 9 brainstorm files
  - Updated DEV_PIPELINE.md with full workflow docs

- **PR flow + in_ci status + need_e2e** (#64): Complete task lifecycle with CI and testing gates.
  - Renamed `IN_REVIEW` → `IN_CI` status; transitions: in_dev → in_ci → testing → done
  - Added `need_e2e` boolean field to Task model (controls smoke vs full E2E testing)
  - `/complete` endpoint auto-promotes through intermediate statuses (in_dev → in_ci → testing → done)
  - Rewrote `/implement` skill: push → PR → CI → smoke/E2E → merge → done
  - Updated `/e2e-run` skill URLs from `/api/tasks/` → `/api/runs/` post-rename
  - Alembic migration for in_ci status rename + need_e2e column
  - 10 new unit tests, 3 flow tests, service test updates

## 2026-03-07

### Changed
- **Rename WorkItem→Task, Task→Run** (#64): Full entity rename across codebase.
  - Planning layer: `WorkItem` → `Task` (table `work_items` → `tasks`, ID prefix `wi-` → `task-`)
  - Execution layer: `Task` → `Run` (table `tasks` → `runs`)
  - API routes: `/api/work-items/` → `/api/tasks/`, `/api/tasks/` → `/api/runs/`
  - Alembic migration renames tables and FK columns in correct order
  - All models, schemas, routers, workers, tests, scripts, and skill files updated
  - ~48 files changed, ~1950 insertions, ~1925 deletions

### Added
- **Milestone model + ROADMAP generation** (#63): Milestones as DB entities to group work items into phases/epics.
  - `Milestone` SQLAlchemy model (id, project_id, title, description, sort_order, status, parent_id, created_by)
  - `MilestoneStatus` DTO with transitions (open -> completed)
  - `POST/GET/PATCH/DELETE /api/milestones/` — CRUD endpoints with project_id/status filters
  - `POST /api/milestones/{id}/complete` — action endpoint with transition validation
  - `GET /api/milestones/{id}/work-items` — sub-resource listing
  - `WorkItem.milestone_id` FK — links work items to milestones
  - `?milestone_id=X` filter on work items list endpoint
  - Alembic migration for `milestones` table + `work_items.milestone_id` column
  - `scripts/generate_roadmap.py` + `make roadmap` — generates ROADMAP.md from API
  - `scripts/seed_milestones.py` — one-time migration of existing ROADMAP phases
  - 33 unit tests (DTO, model, schemas, router, roadmap formatter)
- **Brainstorm model in DB** (#61): Brainstorms as first-class DB entities instead of markdown-only files.
  - `Brainstorm` SQLAlchemy model with status state machine (draft → done → triaged → archived)
  - `POST/GET/PATCH/DELETE /api/brainstorms/` — CRUD endpoints
  - `POST /api/brainstorms/{id}/done|triage|archive` — action endpoints with transition validation
  - `WorkItem.source_brainstorm_id` FK — links work items back to originating brainstorm
  - Alembic migration for `brainstorms` table + FK column
  - Updated `/brainstorm` and `/triage` skills to use API
  - 30 unit tests (DTO, model, schemas, router), 3 integration tests
- **Skills → API + Simplified Model** (#58): All skills now use Work Items API instead of markdown files.
  - `plan` text field on WorkItem model + migration
  - `COMMENT` event type (Jira-style discussion); removed `STEP_START`/`STEP_DONE`
  - `GET /api/work-items/stats` — status counts
  - `GET /api/work-items/next-tag` — next available backlog tag number
  - `GET /api/work-items/?since=<datetime>` — filter by updated_at
  - `project_id` and `plan` fields on `WorkItemUpdate` schema
  - `scripts/generate_backlog.py` + `make backlog` — generate backlog.md from API
  - `docs/ideas.md` — standalone Ideas file (read by make backlog)
  - Updated `/plan`, `/implement`, `/triage`, `/checkpoint` skills to use API
  - `/next` skill removed (absorbed into `/implement`)
  - 12 new service tests for API v2 endpoints
- **`/implement` emits work item events** (#57): `/implement` skill now writes `step_start`/`step_done` events via `POST /api/work-items/{id}/events` at each plan step, and calls `/complete` on task finish. New `step_start`/`step_done` event types in `WorkItemEventType`. `/next` now writes `WorkItem` ID to STATUS.md for downstream skills.
- **`/next` skill via Work Items API** (#56): First skill migrated from markdown parsing to API. `/next` now picks tasks via `GET /api/work-items/?status=backlog&limit=1` and starts them via `POST /api/work-items/{id}/start`.
  - `limit` and `sort` query params on list endpoint
  - `GET /api/work-items/by-tag/{tag}` — lookup by backlog tag (e.g. `#53`)
  - 5 service tests for the `/next` flow
- **WorkItem task management system** (#55): Planning layer for tracking features/fixes with agile statuses (backlog → todo → in_dev → testing → done). Models: `WorkItem`, `WorkItemEvent`. Action-based API with state machine validation. Alembic migration, 25+ unit tests, service tests, backlog migration script.
  - `POST/GET/PATCH/DELETE /api/work-items/` — CRUD
  - `POST /api/work-items/{id}/start|complete|fail|reopen|transition` — state machine actions
  - `GET/POST /api/work-items/{id}/events` — event history
  - `Task.work_item_id` + `Task.iteration` — links execution to planning layer
  - `scripts/migrate_backlog.py` — migrates backlog.md Queue into DB

### Fixed
- **Secrets not persisting**: `POST /projects/{id}/config/secrets` returned 200 but never saved. Root cause: plain `JSON` column didn't detect in-place dict mutations. Fix: `MutableDict.as_mutable(JSON)` on `Project.config` and `project_spec` columns + `dict()` copy in `merge_secrets` (#51)
- **Project stuck in "deploying"**: deploy-worker didn't reset project status on `missing_user_secrets`. Now rolls back to `failed` (#51)
- **API service tests event_loop**: replaced deprecated `event_loop` fixture with `asyncio_default_test_loop_scope=session` to fix "Future attached to a different loop" errors (#51)
- Description loss in create flow: `trigger_engineering` now PATCHes `detailed_spec` into project config for `action=create` (#50)
- `_build_create_task` uses `feature_description` from queue as fallback when `detailed_spec` is missing (#50)
- PO prompt updated to pass description to both `create_project` and `trigger_engineering` (#50)

## 2026-03-06

### Added
- Telegram admin "Add User" button: inline keyboard + text input flow to create users via `POST /users/` (#49)
- `POST /api/projects/{id}/config/secrets` atomic merge endpoint with `SELECT FOR UPDATE` locking (#47)
- `merge_secrets()` method on `LanggraphAPIClient` (#47)
- Concurrent secrets merge integration test (#47)
- `user_name` field on `POUserMessage`; telegram bot populates from `tg_user.first_name` (#45)
- User context injection `[context: user_id=..., user_name=...]` prefix on PO messages (#45)
- `hint` parameter on `set_project_secret` tool; hints stored in `config.env_hints` (#45)
- PO prompt sections: env hints usage, access control question for tg_bot projects (#45)
- `_format_env_hints()` in DeveloperNode — injects `## Provided Environment Variables` into TASK.md (#45)
- Integration test: verify env_hints appear in worker TASK.md (`test_task_injection.py`) (#45)
- PO `web_search` tool: DuckDuckGo search for third-party API documentation (#44)
- System prompt guidance for when to use web search vs. existing knowledge (#44)
- PO Socratic dialog: requirements gathering before triggering engineering (#43)
- PO prompt focuses on product questions for non-technical users, avoids technical details (#43)
- `trigger_engineering` docstring emphasizes passing full gathered spec as description (#43)
- Unit tests for PO prompt content and tool docstrings (#43)

### Fixed
- Corrupted checkpoint recovery: PO consumer auto-repairs orphan tool_calls that block users permanently (#48)
- `ruff.toml` per-file-ignores now covers `**/tests/**` paths (services tests were getting PLR2004 false positives) (#48)
- Race condition in `set_project_secret` when LLM calls it in parallel — secrets no longer lost (#47)
- `test_post_projects_pure_db` integration test — add `X-Telegram-ID` header and seed user via API (#42)

### Changed
- `set_project_secret` PO tool uses single POST instead of GET→decrypt→merge→encrypt→PATCH (#47)
- `_save_secrets_to_project` in devops nodes delegates to `api_client.merge_secrets` (#47)
- `owner_id` on projects is now NOT NULL — every project must have an owner (#39)
- `POST /api/projects/` returns 400 if `X-Telegram-ID` header is missing (#39)
- `github_sync` no longer creates orphan projects — sends admin notification for unknown repos (#39)
- Webhook removes `if project.owner_id` guard — owner always exists (#39)
- `ProjectDTO.owner_id` is now `int` (was `int | None`), `ProjectRead` includes `owner_id` (#39)

### Removed
- `ProjectUpdate.owner_id` field — owner is immutable after creation (#39)
- `SchedulerAPIClient.create_project()` — scheduler no longer creates projects (#39)

### Added
- Workspace failure counter: tracks consecutive failures per project in Redis (#8)
- Force workspace wipe after 2 consecutive failures — broken state auto-recovery (#8)
- Spawn rejection after 3 consecutive failures — circuit breaker with auto-unblock (TTL 48h) (#8)
- `reason` field on `DeleteWorkerCommand` — `completed`/`failed`/`timeout` for failure tracking (#8)
- `--feature` mode in e2e-run skill: triggers `action=feature` after initial create+deploy, verifies no scaffold, monitors feature CI+deploy (#34)
- Feature Add Matrix in e2e-run skill: per-test feature descriptions for all 7 test cases (#34)
- Unit tests for `action=feature/fix` flow in DeveloperNode and engineering worker (#34)
- `GET /projects/by-repo-id/{repo_id}` — lookup project by GitHub repo ID, used by scheduler github_sync (#33)
- `GET /servers/{handle}/ssh-key` — returns decrypted SSH private key per server (#33)
- `PATCH /servers/{handle}` accepts `ssh_key` field — encrypts with Fernet and stores (#33)
- Provisioner auto-saves SSH key to DB after successful provisioning (#33)
- `LanggraphAPIClient.get_server_ssh_key()` — fetches per-server SSH key (#33)
- `_ssh_key_tempfile()` context manager for secure temporary SSH key files (#33)
- `docker-compose.prod.yml` — production overlay (no direct API port, restart policies, Redis AOF, no DB defaults) (#32)
- `infra/scripts/pull-worker-images.sh` — pulls worker base images from GHCR and retags to local names (#32)
- `infra/scripts/backup-db.sh` + systemd timer — daily pg_dump with 7-day rotation (#32)
- `docs/DEPLOY.md` — full production deployment guide with GitHub Secrets inventory (#32)

### Changed
- DeployerNode reads SSH key from DB (per-server) instead of mounted file (#33)
- `run_ssh_command()` accepts `ssh_key` content parameter instead of reading from `Paths.SSH_KEY` (#33)
- `docker-compose.yml`: parameterized `SSH_KEY_PATH` and `GITHUB_APP_PEM_PATH` with dev defaults (#32)
- `.github/workflows/deploy.yml`: complete rewrite — writes all env vars, builds images, pulls worker images from GHCR, runs migrations, health checks (#32)

### Removed
- SSH volume mounts (`~/.ssh:/root/.ssh:ro`) from langgraph, deploy-worker, scheduler, infra-service (#33)
- `Paths.SSH_KEY` from `shared/constants.py` — no longer needed (#33)
- `ORCHESTRATOR_SSH_KEY` secret from deploy.yml — per-server keys in DB now (#33)

### Fixed
- CI: service test matrix `changed` field was literal string, not `${{ }}` expression — tests were silently skipped on every run since #4 (#38)
- API: make `X-Telegram-ID` optional for project creation — system calls (scheduler github_sync) create discovered projects with `owner_id=None` (#38)
- Service test `test_pure_crud`: removed unnecessary `X-Telegram-ID` header (test verifies no side effects, not ownership) (#38)
- Service test `test_service_db_smoke`: fixed event loop mismatch caused by session-scoped DB engine (#38)

## 2026-03-05

### Fixed
- Atomic port allocation: `UniqueConstraint(server_handle, port)` + `POST /ports/allocate-next` endpoint with `SELECT FOR UPDATE` — eliminates TOCTOU race in parallel deploys (#31)

### Removed
- Dead `ports.py` PO tools (`allocate_port`, `get_next_available_port`) and `PortAllocationResult` schema — replaced by atomic allocation in `allocator.py` (#31)

### Fixed
- Multi-user isolation: PO tools now pass `X-Telegram-ID` header in all API calls (#30)
- API requires `X-Telegram-ID` for project creation — prevents orphan projects with `owner_id=NULL` (#30)
- Workers pass user's telegram_id to API when fetching projects, enabling ownership checks (#30)
- `LanggraphAPIClient.get_project()` and `list_projects()` accept optional `telegram_id` param (#30)

### Changed
- Replaced last "Zavhoz agent" reference with "ResourceAllocatorNode" in `AllocatedResource` docstring (#12)
- Clarified engineering-worker and deploy-worker as Redis stream consumers of the langgraph image, not independent services, across CLAUDE.md, README.md, ARCHITECTURE.md (#12)
- CI integration tests: sequential → 5 parallel matrix jobs (backend, cli, template, frontend, infra) (#4)
- Per-suite change detection: each integration suite only runs when relevant files changed (#4)
- Healthcheck intervals 5s→2s in non-DIND test compose files (frontend, infra, cli) (#4)
- Per-suite Docker buildx cache keys for better cache hits (#4)

### Removed
- Dead `list_repos.py` debug script from langgraph service (#17) — 72 LOC, standalone script with `sys.path` hack and `print()`
- Legacy name-based project lookup fallback in github_sync (#17) — `get_project_by_name` from scheduler API client + fallback in `_sync_single_repo`
- Dead CLI agent config infrastructure (#36): `CLIAgentNode`, `cli_agent_config_cache`, CLI agent config API router/schema/ORM model, alembic migration — 423 LOC deleted
- Dead `architect_complete` field from `OrchestratorState` and provisioner init (#37)
- Vestigial references to removed agents (architect, Zavhoz, product_owner, brainstorm, developer) in comments/docstrings (#37)

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
