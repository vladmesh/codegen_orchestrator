# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/). Группировка по датам.

## 2026-07-13

### Changed
- **Typed engineering consume (Sprint 002 Phase 3, `codegen_orchestrator-457`)**: `process_engineering_job` now validates its input with `EngineeringMessage.model_validate(job_data)` before any business logic, mirroring the deploy/qa/architect consumers. Removed the hand-unpacking of 11 fields via `job_data.get(...)` and the fallback defaults for required message data (`task_id`/`user_id`/`action`/`skip_deploy`/`deploy_fix_attempt`), so a malformed job no longer runs with `"unknown"`/`""`/`"create"` placeholders. A `ValidationError` is handled as a terminal poison entry, not a raise: `_handle_invalid_engineering_message` logs only `type`+`loc` (never the raw payload — it carries the user's `description` and ids) and fails the run when `task_id` is present. The entry is ACKed only once that terminal outcome is durably written: if `_fail_job` hits a transient API error (5xx or a transport error) the handler re-raises so the `auto_ack=False`/`claim_pending` loop leaves the entry unacked and retries after the API recovers; only a non-retryable client error (e.g. 404 — no such run) is ACKed, to avoid an eternal poison-loop. `action` comparisons use `ActionType.CREATE`. Tests: `services/langgraph/tests/unit/consumers/test_engineering_validation.py`.

### Removed
- **Dead `langgraph/src/tools/` layer (B5, `codegen_orchestrator-457`)**: deleted `tools/{projects,servers,github,specs}.py`, `tools/__init__.py`, `tools/base.py`, and the dead result models in `schemas/tools.py` (~800 lines that shadowed the live agent tools and eager-imported `ddgs`/`yaml`/github deps). The only live piece, `allocator.py`, moved to `services/langgraph/src/allocations.py`; `nodes/resource_allocator.py` and `consumers/deploy.py` import from there, and `schemas/__init__.py` no longer re-exports the tool models. Guard test: `services/langgraph/tests/unit/test_dead_layer_removed.py`.
- **`worker:lifecycle` stream + contract (`codegen_orchestrator-457`)**: no consumer existed. Removed `WorkerWrapper.publish_lifecycle` and its 3 call sites, the `WorkerLifecycleEvent` contract (`shared/contracts/queues/worker_lifecycle.py`), the `WorkerChannels.LIFECYCLE` member, and the now-unused `WorkerLifecycleKind` vocab slice.
- **Second `agent_config_cache` (`codegen_orchestrator-457`)**: deleted `services/langgraph/src/config/agent_config_cache.py`, a redundant TTL cache stacked on `agent_config.get_agent_config` (which already caches). `nodes/base.py` and `subgraphs/devops/env_analyzer.py` call `get_agent_config` directly.
- **Unreferenced `worker-manager/src/scaffold_phase.py` (`codegen_orchestrator-457`)**: legacy scaffold path with no production callers; removed with its unit test.
- **`shared` compat-shims (`codegen_orchestrator-457`)**: removed the `shared/__init__.py` `try/except → RedisStreamClient = None` swallow (now a direct fail-fast import), the `ServiceDeployment`/`DeploymentStatus` aliases and the legacy `DeploymentStatus` enum in `shared/models`, and the `ensure_consumer_groups` alias in `shared/queues.py`. Guard test: `shared/tests/unit/test_phase3_shims_removed.py`.
- Note: raw `publish`/`publish_flat` on `RedisStreamClient` stay public — ~13 live production producers still call them; the raw API was not extended and per-consumer migration to `publish_message` continues in Phase 3/4.

## 2026-07-12

### Changed
- **Typed `Run.result` (Sprint 002 Phase 2 keystone, `codegen_orchestrator-440`)**: `RunDTO.result` is no longer `dict | None`. Added `shared/contracts/dto/run_result.py` with one `extra="forbid"` model per `RunType` — `EngineeringRunResult` (`engineering_status` + commit/modules/tests), `DeployRunResult` (`deploy_outcome` + deployed_url/application_id/bot_username/deploy_fix_attempt/error_details/action + opaque deployment/smoke blobs), `QARunResult` (`qa_outcome` + summary/`failed_checks: list[QAFailedCheck]`/report/qa_attempt/deployed_url/error). `RunDTO` binds the union to `type` (`_check_result_matches_type`): a payload of the wrong type, an unknown field, an unknown outcome, or a missing required field is rejected at the boundary; `result=None` stays valid for runs with no result yet. Producers (`deploy`, `deploy_result_handler`, `deploy_failure_handler`, `engineering_result_handler`, `qa`) now build the typed model and `model_dump(mode="json")` — one wire form, and the duplicated inline classification→outcome dict in `deploy_result_handler` is replaced by the shared `_classification_to_outcome`. The scheduler supervisor reads `run.result.deploy_outcome` / `.qa_outcome` / typed attributes instead of `run.result or {}` + `.get()` + `DeployOutcome(str)`. `result=None` is allowed only before the outcome exists (`QUEUED`/`RUNNING`/`CANCELLED`); a `COMPLETED`/`FAILED` run without a result is rejected, so a terminal run that lost its outcome surfaces loudly instead of wedging a story forever. All producer failure paths that reach a terminal status now write a typed result — deploy outcomes, `QAOutcome.ERROR` when QA can't resolve its server (previously the run stayed `QUEUED` and the story sat in `TESTING` forever), and `EngineeringStatus.FAILED`/`GAVE_UP` on engineering failure/give-up paths. Migration: `Run.result` stays a JSON column and the API's `RunRead` stays dict-typed (passthrough), so no DB migration and no break on historical reads; in-flight runs parse by construction. The scheduler validates only the latest run per story (`get_latest_run_by_story` parses `rows[0]` alone), so an older legacy/corrupt run can't fail a story whose current run is valid; a latest run that fails validation (wrong-type/corrupt, or terminal-without-result) is routed to a terminal, visible state (`supervisor._fail_story_on_invalid_result`: fail story once + notify admins) instead of poison-looping. `deployed_url`/`application_id` stay optional on `DeployRunResult` (a standalone deploy, or a success where the app record didn't resolve, legitimately lacks `application_id`), but the QA handoff needs both — so `_handle_deploy_success_story` validates them *before* transitioning the story or creating a QA run, routing a `SUCCESS` that can't reach QA to a visible failure instead of crashing the tick after partial state. Contract tests in `shared/tests/unit/test_run_result.py` (each type, `result=None` per non-terminal status, terminal-without-result rejection, cross-type/unknown-outcome/unknown-field/missing-field rejection, optional-field round-trip); scheduler routing/invalid-result/CANCELLED-skip/terminal-no-result/missing-handoff-fields tests split across `services/scheduler/tests/unit/test_supervisor.py` and `test_supervisor_run_routing.py` (shared factories in `_run_routing_factories.py`); latest-only validation tests in `test_api_client.py`; QA server-resolve terminal-result test in `test_qa_consumer.py`. Closes the last slice of Phase 2; next is Phase 3 typed Redis consume.
- **Unified contract vocabularies (Sprint 002 Phase 2, `codegen_orchestrator-436`)**: added `shared/contracts/vocab.py` with one canonical `StrEnum` per cross-service concept — `AgentType` (moved here, re-exported from `queues.worker`), `ActionType`, `ResultStatus`, `LifecycleEvent`. Replaced the competing inline `Literal[...]` sets: `BaseResult.status` (dropped the `error` failure synonym, now `success`/`failed`/`timeout`), `EngineeringMessage.action` (`ActionType`), and `AgentConfigDTO.type` (`AgentType`). `LifecycleEvent` is the canonical member set, but each wire keeps its own supported slice as an explicit `Literal` subset over the enum members — `TaskProgressKind` (`started`/`progress`/`completed`/`failed`) for `ProgressEvent.type` and `WorkerEvent.event_type`, `WorkerLifecycleKind` (`started`/`completed`/`failed`/`stopped`) for `WorkerLifecycleEvent.event` — so the historically different per-field vocabularies are preserved, not merged (a progress stream still rejects `stopped`, the worker-lifecycle stream still rejects `progress`). Worker-side comparisons now use the enum instead of raw `"claude"`/`"factory"` strings (`worker-wrapper` config + wrapper, `worker-manager` container_config/manager/`_get_agent`), and `worker-manager` consumer stops passing `agent_type.value`. The provisioner-result / infra-service producer/consumer and the telegram admin notifier use `ResultStatus`. Because `BaseResult.status` no longer accepts `error`, the scheduler `provisioner:results` consumer now treats a message that fails validation as terminal (`handle_provisioner_entry`: logs it and ACKs) instead of leaving it unacked to poison-loop the `claim_pending` reclaim; transient processing errors still stay unacked for retry. Two historically-mismatched vocabularies are kept distinct on purpose, not merged: `DeployAction` (adds `stop`/`undeploy`) and `TaskType` (adds `refactor`) stay separate from `ActionType`, and `WorkerEvent.worker_type` is now `WorkerCliKind` (`droid`/`claude_code`/`codex`), explicitly disjoint from `AgentType`. Contract tests in `shared/tests/unit/test_vocab.py` (accepted values + rejection of unknowns and of out-of-slice lifecycle values per field) and `services/scheduler/tests/unit/test_provisioner_entry.py` (poison message ACKed, valid processed+ACKed, transient error left unacked). Out of scope (still open in Phase 2/3): `Run.result` union, typed Redis consume / raw-dict consumers.
- **Typed response-DTO lifecycle fields (B7 slice of Sprint 002 Phase 2)**: lifecycle fields on the read-side DTOs now declare their existing `StrEnum` instead of bare `str`, so Pydantic rejects unknown values at the read boundary. `TaskDTO.status/type` and `TaskEventDTO.event_type/from_status/to_status`, `StoryDTO.status/type`, `ServerDTO.status` (+ `ServerCreate.status`), `ApplicationDTO/Create/Update.status`, `IncidentDTO.incident_type/status` (+ `IncidentCreate.incident_type`, `IncidentUpdate.status`), and `ServiceDeploymentDTO.status` (`DeploymentResult`). Dropped the "use str for flexibility" comments. Added accept-valid / reject-unknown unit tests per DTO. Only the B7 response-DTO slice; the duplicated vocabularies and `Run.result` typing from Phase 2 remain open.

### Fixed
- **Worker-mode compose proxy targets**: worker-wrapper now overrides service-template's portless `worker-start` and `worker-stop` targets instead of local-mode `dev-start` and `dev-stop`. Start preserves service filtering and sends `up -d --build --wait`; stop remains project-scoped and does not remove volumes.
- **Pinned production scaffolding**: both scaffold paths now use the typed GitHub source `gh:vladmesh/service-template` and an explicit system-config ref, baseline `0.3.0`. Removed the unused local template mount, reject floating refs, and record Copier's resolved commit for reproduction.

## 2026-07-11

### Changed
- **Normalize CI merge gate**: added stable `Required CI Gate` job, mandatory CI contract check, unconditional format/lint/unit checks, expanded service and integration routing for shared/test/dependency/workflow changes, and explicit assertions that required matrix test commands actually ran before a matrix job can pass.

## 2026-07-10

### Fixed
- **API service tests after internal auth hardening**: service-test compose now provides `INTERNAL_API_KEY` to both the API container and test runner, and API service test clients send `X-Internal-Key` by default. This keeps server/project/run test setup aligned with the fail-closed internal auth contract. `make test-service SERVICE=api` now also checks compose container exit codes before cleanup, so dependency startup failures cannot be reported as a green test run.

## 2026-05-29

### Changed
- **Upgrade redis-py to 8.0.0 + make the consumer layer compatible**: bumped `redis[hiredis]` to `>=8.0.0` across all services and pinned `redis==8.0.0` in the `requirements.lock` files. redis-py 8 stopped applying `decode_responses=True` to the field maps returned by `XREADGROUP` / `XREAD` / `XAUTOCLAIM` and `XINFO GROUPS`, and to `HGETALL` (they now come back as `bytes`), while `XRANGE` / `HGET` / `SMEMBERS` etc. still decode. Added `decode_redis_value` / `decode_redis_fields` helpers in `shared/redis` and normalized every affected read: `RedisStreamClient.consume`/`_recover_pending`, PO consumer (`po.py`), telegram-bot `XREAD`, API debug router (`XINFO GROUPS`), and worker-manager `HGETALL` sites (`manager.py`, `introspect.py`, `garbage_collector.py`). Also hardened `consume()` to cede the event loop on empty reads (fakeredis ignores the block timeout, which would otherwise busy-spin and starve the loop). Without this the entire consumer layer silently broke on redis 8 (messages dropped on validation, JSON never unwrapped). Tests updated to read through the decode boundary; added a `_parse_fields` bytes-decoding regression test.
- **QA tester prompt moved into `prompts/` package**: extracted `build_qa_prompt` from `services/langgraph/src/consumers/_qa_runner.py` into a dedicated `services/langgraph/src/prompts/qa/__init__.py`, consistent with the `architect`/`po`/`developer_worker` prompts. `_qa_runner.py` now imports the builder. No behavior change (prompt text preserved verbatim).

## 2026-04-09

### Added
- **Stale queue message cleanup** (#1021): Centralized staleness guard in `_base.py` — before processing, consumers check if the referenced run/story is terminal (COMPLETED/FAILED/CANCELLED/ARCHIVED). Stale messages are ACKed and skipped instantly, preventing the 75-message flood that blocked the 2026-03-13 escort for hours. New `queue_cleanup_worker` in scheduler runs every 10 minutes: cleans orphan `po:response:*` and `worker:*:input/output` streams idle >10min, trims entries >7 days from all task queues via XTRIM MINID. Architect's duplicate guard simplified to DEPLOYING only. 20 unit tests.

## 2026-03-21

### Added
- **ЛК frontend SPA** (#1036): New `services/user-dashboard/` — React + Vite + Tailwind CSS + Recharts SPA for non-technical founders. Auth flow (one-time token → JWT), project list with summary metrics, project dashboard with period selector (24h/7d/30d), 4 KPI cards, line chart with metric switching, service status, top endpoints, per-service breakdown. Docker: node build → nginx:alpine, port 3003. Light theme, Russian UI, mobile-responsive.
- **LK API: auth + analytics endpoints** (#1034): JWT auth flow via `POST /api/lk/auth/token` (one-time Redis token → 24h JWT). 4 owner-scoped analytics endpoints: projects list with daily summary, project summary, chart data, service status. 18 service tests.
- **Telegram bot: dashboard button** (#1035): `/dashboard` command generates one-time Redis token (TTL 5min) and sends inline URL button to open the LK dashboard.

## 2026-03-20

### Added
- **Add com.codegen.project_id label to deployed containers** (#1032): Deployer injects `CODEGEN_PROJECT_ID` into the deployed `.env`. service-template `compose.prod.yml` adds `com.codegen.project_id` Docker label to all prod services. Promtail on prod servers (from #1031) discovers containers by this label and extracts `project_id` as a Loki label for per-project log filtering.
- **Promtail on prod servers + expose Loki** (#1031): Expose Loki via Caddy `/loki/*` with Basic Auth for external Promtail push. New Promtail Ansible template scrapes Docker containers by `com.codegen.project_id` label and ships logs to orchestrator over HTTPS. Added Promtail service to monitoring role docker-compose. Pass `orchestrator_hostname` through AnsibleRunner extra-vars. New env vars: `LOKI_PUSH_USER`, `LOKI_PUSH_PASSWORD`, `LOKI_PUSH_PASSWORD_HASH`.

## 2026-03-19

### Added
- **Regression E2E: acceptance criteria on Repository + QA report in admin UI** (task-a775d789): `acceptance_criteria` (Text) and `bot_username` (String) fields on Repository model. Architect agent gets `update_acceptance_criteria` tool — updates repo criteria after story decomposition for regression testing. QA consumer now uses repo `acceptance_criteria` instead of story description — tests full product behavior, not just latest feature. PO agent writes `bot_username` to repo during Telegram token validation. `run_e2e` admin endpoint passes `bot_username` from repo in QAMessage. Admin UI ApplicationDetailPage shows QA run outcome badge + expandable report with failed checks and full markdown. New `GET /applications/{id}/runs` endpoint. Seeded all 4 existing repos with acceptance criteria and bot usernames.

### Added
- **Admin UI: action buttons on entity pages** (#1026): Action buttons across all admin SPA entity pages. New `ConfirmButton` reusable component. TaskDetailPage: Spawn Worker button. New StoryDetailPage with Send to Architect button. New ApplicationDetailPage with Stop/Undeploy/Redeploy/Run E2E buttons + health history table. ProjectDetailPage: secrets editor (masked key-value, add/delete), Create Story form, Deploy from Repo form. New routes `/stories/:id` and `/applications/:id`. New `GET /projects/{id}/config/secrets/keys` API endpoint.
- **Thin API endpoints for admin actions** (#1024): 8 new endpoints on the API service. `POST /stories/{id}/send-to-architect` (validate+transition+publish ArchitectMessage). `POST /tasks/{id}/spawn-worker` (transition+create Run+publish EngineeringMessage). `POST /applications/{id}/stop|undeploy|redeploy` (status transitions + publish DeployMessage). `POST /applications/{id}/run-e2e` (create Run + publish QAMessage). `POST /applications/from-repo` (create Repo+App+port+deploy). `DELETE /projects/{id}/config/secrets/{key}`. Added `ADMIN` to `DeployTrigger`, `DEPLOYING/STOPPING/UNDEPLOYING` to `ApplicationStatus`. API now has `RedisStreamClient` singleton for queue publishing.
- **Queue contracts: Optional story_id + action field** (#1023): `DeployAction` StrEnum replaces `Literal["create", "feature", "fix"]` — adds `stop` and `undeploy` values for lifecycle operations from admin. `QAMessage.story_id` now optional (defaults to `""`) for standalone E2E triggers. New `deploy_lifecycle` module handles stop/undeploy via SSH, skipping the full DevOps subgraph. QA consumer uses `application_id` for inflight dedup when no story. Engineering and architect consumers verified compatible with direct publish.

### Changed
- **Decouple QA consumer from story lifecycle** (#1030): QA consumer stripped of all story transitions (`_transition_story_safe`, `publish_story_event`) and fix task creation. Now only updates `run.status` and `run.result` with a `QAOutcome` enum (PASSED/FAILED/EXHAUSTED/ERROR). Added `RunType.QA` and `QAMessage.run_id` to shared contracts. Dispatcher creates QA run before publishing QAMessage. New `supervise_testing_stories()` in dispatcher polls TESTING stories, reads QA run outcome, and routes: PASSED → complete story, FAILED → create fix task + redispatch to engineering, EXHAUSTED/ERROR → fail story. QA is now a pure technical worker, same pattern as deploy (#1006).
- **Decouple deploy worker from story lifecycle** (#1006): Deploy worker stripped of all story transitions (`_transition_story_safe`, QA handoff, retry tracking, admin notifications). Now only updates `run.status` and `run.result` with a `DeployOutcome` enum (SUCCESS/SMOKE_FAILURE/CODE_FIX/RETRY/GIVE_UP). New `supervise_deploying_stories()` in dispatcher polls DEPLOYING stories, reads deploy run outcome, and routes: SUCCESS → TESTING + QA, CODE_FIX → engineering redispatch, RETRY → redeploy with counter, GIVE_UP → FAILED + admin notification. Added `story_id` FK to Run model for efficient querying. Deploy is now a pure technical worker callable outside story context.

### Added
- **Admin UI: Settings page** (#1025): New `/settings` page in admin SPA with two tabs. System Configs tab shows all configs grouped by category (scheduler, supervisor, deploy, health, llm) with inline edit per row. Agent Configs tab shows expandable cards with prompt textarea editor, model/temperature fields. Sidebar navigation item added.
- **SystemConfig model + API + ConfigStore** (#1020): New `system_configs` DB table for externalizing operational constants. CRUD API at `/api/system-configs/`. `ConfigStore` client in shared/ with TTL cache. Seed script populates 29 defaults from YAML (`make seed`). Scheduler validates all required configs at startup (fail-fast). Replaced hardcoded constants in 12 scheduler/langgraph task modules with DB-backed values.

### Changed
- **Unified worker result API** (task-1b2bdf73): Replaced three HTTP endpoints (`/complete`, `/failed`, `/blocker`) with single `POST /result` accepting `{success: true/false}`. Added `/infra/compose` proxy route so workers use only `localhost:9090`. Captures agent stdout tail (~10KB) for debugging. Auto-resumes Claude agents once if they exit without calling `/result`. Merged `reject_reason`/`block_reason` into `gave_up_reason` across SpawnResult, developer node, and engineering consumer.
- **EngineeringStatus StrEnum** (task-9f294c98): Replaced 6 bare `engineering_status` strings with `EngineeringStatus(StrEnum)` — 4 values: IDLE, DONE, GAVE_UP, FAILED. Merged `_handle_worker_blocked` + `_handle_worker_reject` → `handle_worker_gave_up` (both → WAITING_HUMAN_REVIEW). FAILED is transient: supervisor retries or escalates to GAVE_UP when retries exhausted. Removed `NON_RETRYABLE_REASONS` — semantics encoded in task status. Fixed bug where `block_reason` path returned `"blocked"` indistinguishable from generic crash.

### Fixed
- **Restored Makefile overrides in worker-wrapper** (task-ae3ca2fb): Re-added `_inject_makefile_overrides()` removed in b8864abd. Override targets now `curl localhost:9090/infra/compose` (compose proxy from task-1b2bdf73) instead of deleted `orchestrator` CLI. Fixes `make migrate`, `make dev-start svc=db` inside worker containers.
- **QA consumer resolves wrong application** (task-611d788f): QA consumer called `list_applications({"project_id": ...})` but the API has no `project_id` filter — returned ALL applications, picked wrong one (e.g. codegen_orchestrator instead of weather-bot). Now threads `application_id` from deployer → deploy result → QAMessage → QA consumer, using single `GET /applications/{id}` instead of broken list+filter. Also: fix tasks now created with `status=todo` (was defaulting to `backlog`, so dispatcher never picked them up). Replaced dict soup with `QAServerInfo` dataclass and `ApplicationDTO` for typed API responses.

## 2026-03-18

### Removed
- **orchestrator-cli package** (task-b2401a6e): Deleted `packages/orchestrator-cli/` entirely (~1500 lines). Agent now reports results via curl to localhost:9090 (`/complete`, `/failed`, `/blocker`) and manages infrastructure via curl to worker-manager compose proxy. Updated INSTRUCTIONS.md, Dockerfile, container env vars (removed `ORCHESTRATOR_API_URL`, `ORCHESTRATOR_REDIS_URL`, renamed `ORCHESTRATOR_WORKER_MANAGER_URL` → `WORKER_MANAGER_URL`). Removed `shared/schemas/tool_groups.py` and `tool_registry.py` (CLI doc generation, unused by services). Cleaned up CI matrix, test scripts, and docs.
- **result_parser from worker-wrapper** (task-dc3de88a): Deleted `result_parser.py` and stdout-based result parsing (`<result>` tags, `## REJECTED`, `## BLOCKED` markers). Agent results now flow exclusively through HTTP server (localhost:9090). Simplified `execute_agent()` to subprocess lifecycle only. Removed unused `_get_git_head()` and `_extract_git_commit_sha()`. Watchdog intact: agent exits without HTTP call → auto-fail.

### Added
- **HTTP result server in worker-wrapper** (task-7397ff9b): Added localhost:9090 HTTP server that runs alongside the agent subprocess. Three POST endpoints (`/complete`, `/failed`, `/blocker`) with Pydantic validation — agent gets 400 on bad payload (can retry), 409 on duplicate. HTTP result takes priority over stdout parsing; backward compatibility preserved. Watchdog auto-publishes `failed` if agent exits without reporting. First step of decoupling workers from shared package.

### Fixed
- **Architect 422 on story.start** (hotfix): Architect LLM tool `transition_story` now catches 422 and returns current story state instead of crashing. Fixes race where PO already transitioned story to `in_progress` before architect runs.
- **Deploy failure classifier blind** (hotfix): `wait_for_run_completion` now fetches GH Actions failure logs (`get_workflow_failure_logs`) and includes failed job/step names in the RuntimeError message. The deploy classifier now sees "Job 'deploy' failed: Step 'Deploy via SSH' failed" instead of just "failure".
- **Deploy retry loop after PR merge** (hotfix): `create_pull_request` now searches closed/merged PRs when 422 occurs (was only searching open). `complete_stories` detects merged PRs and triggers deploy directly instead of trying to create a duplicate PR. Breaks the infinite loop: deploy fail → in_progress → create PR (422) → exception → retry.

## 2026-03-17

### Added
- **Shared Pydantic DTOs for API entities** (task-a2a69435, steps 1-3): Added response+request DTOs (`TaskDTO`, `TaskCreate`, `TaskUpdate`, `TaskEventDTO`, `TaskEventCreate`, `StoryDTO`, `StoryCreate`, `StoryUpdate`, `RepositoryDTO`, `RepositoryCreate`, `RepositoryUpdate`, `ApplicationDTO`, `ApplicationCreate`, `ApplicationUpdate`, `IncidentDTO`, `IncidentCreate`, `IncidentUpdate`) to `shared/contracts/dto/`. Moved `IncidentStatus`/`IncidentType` enums from model to DTO (model re-exports for backward compat). 41 new unit tests. Client migration follows in steps 4-9.

### Changed
- **Migrate API clients to Pydantic DTOs** (task-a2a69435, steps 4-9): Migrated all service API clients from raw `dict` returns to typed Pydantic DTOs. Scheduler: 29 methods typed, ~80 caller sites migrated. LangGraph: 17 typed methods, ~80 caller sites migrated (generic `get/post/patch/delete` kept as dict for LLM-facing tools). Scaffolder: `get_project()` → `ProjectDTO`, `get_repository()` → `RepositoryDTO`. Infra-service: `get_server()` → `ServerDTO`. All `["field"]`/`.get("field")` access patterns replaced with attribute access. 250+ unit tests passing across 9 services.
- **Refactor large files (>400 LOC) — extract helpers** (task-d103f639): Extracted helpers from 10 files exceeding 400 LOC. `manager.py` 920→543 (garbage_collector, git_ops, scaffold_phase), `engineering.py` 881→299 (engineering_result_handler, story_context), `deploy.py` 867→383 (deploy_failure_handler, deploy_result_handler, deploy_precheck), `task_dispatcher.py` 738→265 (story_completion, supervisor, pr_poller), `rag.py` 689→269 (rag_ingest, rag_search), `node.py` 642→391 (operations, handlers), `devops/nodes.py` 639→61 (secret_resolver, deployer), `tasks.py` 625→384 (_task_helpers, _task_actions), `po/tools.py` 605→191 (tools_shared, tools_projects, tools_stories), `developer.py` 513→397 (developer_tasks). All original modules re-export via `__all__` for backward compatibility.

### Fixed
- **Contract violations from audit** (hotfix): Replaced hardcoded `"todo"` with `TaskStatus.TODO.value` in webhooks, removed `os.getenv("API_URL", default)` (fail fast), replaced hardcoded queue name strings with `shared/queues.py` constants in projects router, replaced hardcoded status strings with `TaskStatus` enum in engineering consumer, centralized `STORY_WORKERS_KEY` in `shared/queues.py` (was duplicated in langgraph + scheduler).
- **cadvisor parser: cgroup v2 Docker containers filtered out** (hotfix): `_is_real_container` rejected all containers with `id` starting with `/system.slice`, but on cgroup v2 (systemd) Docker containers have `id=/system.slice/docker-<hash>.scope`. Fix: allow `/system.slice` entries that contain `/docker-` in the path. This was causing the Containers tab in the admin UI to show no data despite 22 containers running.

### Added
- **HTTP health prober for deployed applications + SSL expiry check** (task-d378415c): New `app_health_prober.py` — probes each deployed application's `/health` endpoint via HTTP, tracks response times, consecutive failure detection (SERVICE_DOWN incident after 3 fails), SSL cert expiry monitoring (SSL_EXPIRING incident within 7 days), auto-resolves incidents on recovery, computes 24h uptime%. New `ssl_checker.py` — socket-based SSL cert expiry extraction. Added `SSL_EXPIRING` to `IncidentType` enum. Extended `SchedulerAPIClient` with application CRUD methods. Integrated into existing `health_check_worker` loop (runs after server checks). App health history cleanup in daily job. 30 new unit tests + integration tests.
- **Admin UI: application health status and response times** (task-fb032b50): Extended Application model with `response_time_ms`, `ssl_expires_at`, `uptime_pct_24h` fields. New `application_health_history` table (time-series, 7-day retention) with GET/POST/DELETE API endpoints. Enhanced admin applications table with health status dot, response time, uptime %, SSL expiry columns. Expandable application rows with overview cards and response time area chart (1h/24h toggle via Recharts). 19 unit + 4 integration tests.
- **Admin UI: extended server health dashboard** (task-204ef921): Rewrote ServersPage with tabbed expandable rows — Overview (health summary cards: CPU, load avg, network errors, containers, uptime + freshness indicator), Containers (per-container CPU/RAM table from cadvisor metrics), Charts (CPU/RAM/Disk area charts via Recharts with 1h/24h toggle), Incidents (history table with status badges). Added CPU usage bar to main table. New types: MetricsHistoryEntry, ContainerMetrics, Incident. New utils: formatBytes, formatUptime, freshnessColor. First Recharts usage in the project.
- **Health checker worker** (task-47f2fc7c): Implemented `health_check_worker` with HTTP polling of node_exporter (:9100) and cadvisor (:8080) for managed+active servers. Parses Prometheus metrics via existing parser, updates Server health fields (CPU%, load avg, RAM/disk, container counts, uptime), appends metrics history snapshots. Auto-creates `SERVER_UNREACHABLE` incidents on HTTP failure and `RESOURCE_EXHAUSTED` on RAM/disk >90%, with dedup (no duplicates while active incident exists). Auto-resolves unreachable incidents on recovery. Telegram admin notifications on incident creation/resolution. Daily cleanup of metrics history >7 days. New API endpoint: `DELETE /api/servers/metrics-history`. Extended SchedulerAPIClient with incident + metrics history methods. 17 unit tests.
- **Prometheus text format parser** (task-58d52adf): Pure parser module for node_exporter + cadvisor `/metrics` endpoints. Generic `parse_prometheus_text()` handles the full exposition format (labels, timestamps, scientific notation, +Inf, NaN). `extract_node_metrics()` computes CPU% from idle ratio, RAM/disk from `/proc` values (root mount only), load avg, uptime, network errors. `extract_container_metrics()` groups cadvisor data per container, filters system entries. Public API: `parse_node_exporter(text)` and `parse_cadvisor(text)`. 38 unit tests + realistic fixture-based integration tests.
- **Server health metrics model + history table** (task-107966ae): Extended Server model with 9 health metric columns (cpu_usage_pct, load_avg_1m/5m/15m, network_rx_errors/tx_errors, container_count_running/total, uptime_seconds). New `server_metrics_history` table for 7-day retention time-series snapshots (server_handle FK, recorded_at, metrics JSON, composite index). Updated ServerDTO/ServerUpdate/ServerRead schemas. New API endpoints: `GET/POST /{handle}/metrics-history`. PATCH handler accepts health fields + last_health_check. 19 unit + 4 integration tests.
- **Provisioning: node_exporter + cadvisor + UFW rules** (task-a0a40102): Extended monitoring Ansible role with cadvisor container alongside existing node_exporter. UFW rules restrict ports 9100/8080 to orchestrator IP only (`ORCHESTRATOR_PUBLIC_IP` env var). Monitoring role now included in `provision_software.yml` after Docker setup. `AnsibleRunner` passes `orchestrator_ip` as Ansible extra var. Server vps-267180 configured and verified — both `/metrics` endpoints return data. 19 unit tests.

## 2026-03-16

### Fixed
- **Deploy failure classification and worker rejection pipeline** (task-3a06bf14): Fixed broken classifier model ID (`claude-haiku-4-5-20251001` → `claude-haiku-4-5`). Replaced binary CODE/INFRA classification with three-way CODE_FIX/RETRY/GIVE_UP. Changed fallback from CODE to RETRY (safer — retrying wastes less than spawning a useless worker). Added GIVE_UP handler (story→failed, admin notified, worker deleted). Wired up worker rejection pipeline: DeveloperNode now checks `reject_reason` → sets `worker_rejected` status → engineering consumer routes to `_handle_worker_reject()` (was dead code). Added reject-first sanity check as Step 0 in worker INSTRUCTIONS.md.
- **service_deployments `updated_at` missing server default** (hotfix): Original migration `73b707900b42` created the `service_deployments` table with `created_at DEFAULT now()` but `updated_at` without a default, causing `NotNullViolationError` on every INSERT. Deploy-worker's `_create_deployment_record` silently failed (caught exception). Migration `42e0acc86b20` adds the missing `server_default=now()`.

### Removed
- **Prompts tab in admin panel** (hotfix): Removed the "Prompts" tab from worker detail page, `/prompts` and `/prompt-history` API endpoints, Redis persistence of `task_md` and `prompt_history`, and related tests/types. The `-p` argument is now a hardcoded constant — no value in tracking it.

### Changed
- **Deploy → QA handoff** (hotfix): Deploy consumer now transitions story to `TESTING` and publishes `QAMessage` to `qa:queue` instead of completing story directly. Worker container not deleted (QA may need it for fixes). Standalone webhook deploys (no story_id) bypass QA.

### Added
- **Ansible role: qa_runner provisioning** (task-b6e972e4): New Ansible role that provisions prod servers for QA. Installs Claude Code CLI (standalone binary, no Node.js), telethon+httpx in venv. Creates 2GB swap (prevents OOM on 4GB servers). Auth via `.credentials.json` copy (same session pattern as worker-manager). Included in `site.yml` and `provision_software.yml`. Tested on prod — Claude Code responds. 13 unit tests.
- **QA consumer skeleton** (task-22130356): Post-deploy QA consumer that reads from `qa:queue`, SSHes to prod server, runs Claude Code with story-based QA prompt, and parses the JSON result. Pass → story completed + user notified. Fail → fix task created + story rolled back to `in_progress`. Inflight dedup (25 min TTL), max 2 QA→Engineering loops. `qa-worker` service in docker-compose. 24 unit tests.
- **TESTING story status + QA queue contract** (task-4dbe7a76): Foundation for post-release QA. New `StoryStatus.TESTING` enum value with transitions `DEPLOYING → TESTING → {COMPLETED, IN_PROGRESS, FAILED}`. New `POST /api/stories/{id}/test` endpoint. `QAMessage` contract in `shared/contracts/queues/qa.py`. `QA_QUEUE` + `QA_GROUP` constants and topology binding. 15 new tests.
- **PR merge polling** (hotfix): Dispatcher now polls GitHub for merged PRs on stories in `pr_review` status every 30s. Eliminates dependency on GitHub webhook for the `pr_review → deploying` transition. New `list_pull_requests()` method on `GitHubAppClient`.
- **Deploy failure LLM classifier** (hotfix): Deploy worker now classifies failures as CODE vs INFRA using haiku before dispatching to engineering. INFRA failures (timeouts, network, resource limits) retry deploy instead of wasting an engineering worker. After max retries, story is marked failed for HITL. Extracted `_track_deploy_retry()` helper from `_handle_deploy_failure()`.

## 2026-03-15

### Added
- **Branch protection after scaffold** (task-709e1861): After scaffolder creates a repo and pushes initial commit, GitHub branch protection rules are now set on `main` — requires PR for merge, requires `ci` status check to pass. Non-fatal: scaffold succeeds even if protection setup fails. New `update_branch_protection()` method on `GitHubAppClient`.

### Added
- **Feature branches for stories** (#1011): Workers now operate on story-level feature branches (`story/{story_id}`). Branch name flows through the full pipeline: engineering consumer → developer node → worker spawner → task dispatcher → worker manager → worker wrapper. Worker manager creates/checkouts the branch in containers. Worker wrapper reports branch in result dict and pulls from current branch instead of hardcoded `main`. INSTRUCTIONS.md updated to encourage pushing on feature branches.
- **PR-based CI gate** (#1014): Replaced polling-based CI gate (`_ci_gate.py`, 531 lines deleted) with a PR-based flow. When all story tasks complete, task dispatcher creates a PR from `story/{id}` → `main` and enables auto-merge. CI runs on the PR; green CI → auto-merge → webhook → deploy. Red CI on story branch → webhook creates fix task and transitions story back to `in_progress`. New `PR_REVIEW` story status. Added 4 GitHub client methods (`create_pull_request`, `enable_auto_merge`, `merge_pull_request`, `close_pull_request`). Webhook handler extended to handle `pull_request` (merged) and `workflow_run` (CI failure on story branches) events.

### Changed
- **TASK.md moved to /workspace/**: TASK.md now lives in the workspace directory (`/workspace/TASK.md`) instead of `/home/worker/TASK.md`. Worker-manager injects it there on create; wrapper updates it each turn. After task completes, wrapper archives TASK.md + REPORT.md into `.story/old_tasks/{task_id}.md` — next worker sees full history. `.story/` is auto-gitignored.
- **Minimal `-p` prompt for Claude workers**: Wrapper now passes a one-line redirect ("Read TASK.md") as `-p` instead of the full task content. Full task stays in TASK.md file — Claude reads it on demand, keeping context window clean. Removed self-referential TASK.md references from developer.py and INSTRUCTIONS.md.
- **Merge AUDIT_REPORT.md into REPORT.md**: Removed separate AUDIT_REPORT.md concept from e2e-run skill. Workers already write REPORT.md with Issues+Suggestions sections (per INSTRUCTIONS.md) — that IS the audit report. Worker reports collected via task events API.
- **Filter scaffolder tree output**: `_capture_tree()` now excludes `.venv`, `node_modules`, `.git`, `__pycache__`, `.mypy_cache`, `.ruff_cache` from the tree passed to the architect. Same exclusion set as the admin panel workspace browser. Saves tokens in architect context.
- **E2E skill: save reports before cleanup**: Step 7 now explicitly saves worker reports to local files before Step 9 DB cleanup. Previously reports could be lost when task_events were deleted.

### Added
- **Task archiving (`.story/old_tasks/`)**: After each task, wrapper merges TASK.md + REPORT.md into `.story/old_tasks/{task_id}.md`. Next worker can browse previous tasks for context without force-fed story_context in the prompt.
- **Hybrid --resume session management**: `SessionManager.clear_session()` method + `clear_session` flag in task messages. `send_task_to_worker()` accepts `clear_session=True` to force fresh Claude CLI session on retries (avoids inheriting errors from failed previous attempt). First task in story: fresh (new worker). Subsequent: `--resume` via stored session.

## 2026-03-14

### Changed
- **Bind PortAllocation to Application** (#task-199b1bcb): PortAllocation now belongs to Application (via `application_id` FK) instead of Project. Application no longer has a single `port` field — ports come from `port_allocations` relationship (one-to-many). `ApplicationRead` API response includes `ports: list[PortAllocationRead]`. Application is created at allocation time (before deploy). Deploy flow simplified — uses state data instead of re-querying allocations.

### Added
- **Application entity + Deployment refactor** (#task-f01a41fe): Introduced `Application` as a first-class runtime entity (repo + server + status), separated from `Deployment` (immutable deploy log). New `ApplicationStatus` enum (not_deployed, running, stopped, down, degraded) and `DeploymentResult` enum (pending, success, failed, canceled). Application CRUD API at `/api/applications/`, server applications endpoint at `/servers/{handle}/applications`. DeployerNode now creates Application records on deploy. Data migration backfills Applications from existing deployments. Admin Servers page shows Applications instead of raw deployment records. 24 new unit tests.
- **Tasks page multi-select filters + sortable columns**: Status and type filters now support multi-select (checkboxes). Status, Priority, Updated column headers are clickable for asc/desc sorting. New `MultiSelect` UI component.

### Changed
- **Unified workspace management around repo_id** (#task-7147c381): All workspace addressing now uses `repo_id` instead of `project_id`. Scaffolder is sole source of truth for workspaces at `/data/workspaces/{repo_id}/`. Removed legacy `WORKSPACE_BASE_PATH` config and `/tmp/codegen/workspaces` volume from worker-manager. Workers now require `repo_id` (RuntimeError if missing). `repo_id` stored in Redis `worker:meta` hash and exposed on introspect API. Workspace browser endpoints use `repo_id`. Frontend resolves `repo_id` via repositories API. Removed dead in-container scaffold phase code. Fixes workspace browser not showing files for projects like lesswrong-random-bot.

## 2026-03-13

### Added
- **Ensure-workspace gate** (#task-0bca0e67): Scaffolding now always runs as a gate before pipeline proceeds. `ScaffoldMessage` gains `mode` field (`full`/`ensure`). New `run_ensure_workspace()` — skips if workspace exists, clones+setups if repo exists on GitHub, errors otherwise. `scaffold_trigger` handles ACTIVE projects with TODO tasks (mode=ensure). `task_dispatcher` checks `workspace_ready` flag before dispatching. Worker-manager GC calls new `POST /repositories/{repo_id}/notify-workspace-deleted` API endpoint to clear `workspace_ready` on deletion. Integration tests in infra suite. Fixes crash when workspace is GC'd and pipeline tries to proceed without it.
- **Workspace browser** (#task-a8f3703f): Workspace as first-class entity keyed by project_id. New `/api/introspect/workspaces/{project_id}/tree` and `/files/{path}` endpoints in worker-manager. Shared `FileTree`/`FileViewer`/`WorkspaceBrowser` React components extracted from WorkerDetailPage. ProjectDetailPage gains "Workspace" tab for browsing project files. Worker Files tab delegates to project workspace when available, falls back to Redis meta for ephemeral workers. 12 new unit tests.
- **Admin SPA: LLM Tracing + Users pages** (#task-df069084): New `/tracing` page with Langfuse iframe. New `/users` page (list) and `/users/:id` detail page with projects tab and tracing tab. Sidebar gains "Users" and enabled "LLM Tracing" items. Project detail page shows Owner link and LLM Tracing section. API `GET /projects/` supports `owner_id` query param filter. Nginx strips `X-Frame-Options`/`Content-Security-Policy` from Langfuse proxy to enable iframe embedding.
- **LangChain → Langfuse tracing integration** (#task-300f55e6): Drop-in LLM tracing via `langfuse` v4 SDK. New `src/tracing.py` utility returns LangChain `CallbackHandler` when `LANGFUSE_PUBLIC_KEY` + `SECRET_KEY` env vars are set (empty = disabled). Wired into all 4 consumers (PO, architect, engineering, deploy) via `config={"callbacks": ...}`. Zero changes to agent/graph code. Env vars added to `.env.example`, picked up by all services via `env_file`.
- **Langfuse v3 infra** (#task-a51fb1cf): Self-hosted LLM tracing stack. Docker-compose adds 4 new services: `langfuse-web` (UI on port 3002), `langfuse-worker` (background processor), `clickhouse` (trace analytics), `minio` (S3-compatible event/media storage). Separate `langfuse` PostgreSQL database via init script. Shared Redis (no auth). Nginx proxy at `/langfuse/` through admin-frontend. `make init-langfuse-db` for existing deployments. Env vars for ClickHouse, MinIO, and Langfuse secrets in `.env.example`.

### Fixed
- **Admin tab state lost on refresh**: Detail pages (Project, Queue, User, Worker) now persist active tab in URL search params. WorkspaceBrowser tree auto-refreshes every 15s. User messages trace polling set to 7s.
- **Audit cleanup**: Use enums (`WorkerStatus.STARTING`, `RunStatus.FAILED/RUNNING`), proper exception chaining (B904), `HTTPStatus.BAD_REQUEST` in telegram handlers, fail-fast on missing `API_BASE_URL` in infra-service.
- **Worker lifecycle cleanup**: `delete_worker()` now cleans `worker:{id}:input/output` streams (were orphaned forever). Orphan GC does reverse check (Redis → Docker) — cleans stale `worker:status` entries where container is gone. Deploy consumer deletes worker container on story complete/fail and calls `clear_story_worker` (was dead code). Workspace GC scans both `WORKSPACE_BASE_PATH` and `SCAFFOLDED_WORKSPACE_PATH`, max_age raised to 35h, cleans stale `workspace:active_projects` entries. Introspect API shows GONE status for stale workers.
- **Architect story spam**: Architect consumer now transitions story to `IN_PROGRESS` immediately on pickup, preventing supervisor from re-publishing the same story every 30s. Also skips stories already decomposed (IN_PROGRESS + has tasks). Supervisor retry counter moved from in-memory dict to Redis (`story:architect_retries:{id}` with 1h TTL) — survives scheduler restarts.

### Added
- **Queue message browser**: New `/debug/queues/{stream}/messages` and `/{stream}/{group}/pending` API endpoints. Queue cards in admin are now clickable → detail page with Messages tab (XRANGE, parsed data preview, expandable JSON, delete with confirmation) and Pending tab (consumer, idle time, delivery count, ack button). Also: `POST ack`, `DELETE message` endpoints.
- **WorkerStatus enum** (`shared/contracts/dto/worker.py`): New `StrEnum` with RUNNING, PAUSED, DEAD, FAILED, STOPPED, GONE, UNKNOWN. Replaced all hardcoded status strings across worker-manager (manager, events, introspect router) and langgraph (worker_spawner). Updated all tests.
- **Admin Phase 2: worker inspector + queues + action buttons** (#task-6d8257e5): Workers list page with live auto-refresh (5s), status badges, project links. Worker detail page with tabbed view: Console (live container logs with tail selector), Prompts (CLAUDE.md + TASK.md viewer), Files (collapsible directory tree + file content viewer with size display). Kill worker button with confirmation dialog. QueuesPage upgraded with proper `DebugQueuesResponse` types (bindings array, status badge, issues warning banner). Task detail page gets Retry button (failed → backlog) and Resume button (WHR → in_dev with guidance textarea). API client extended with `rawDelete`/`rawPost` methods. Full TypeScript types for worker-manager introspection API.
- **Worker-manager introspection API** (#task-716e9208): New `/api/introspect/` router in worker-manager with 7 endpoints — list workers, worker detail (with container info from Docker), container logs (tail param, max 5000), workspace file tree, file content (with path traversal protection via symlink-safe resolve), prompts (CLAUDE.md + TASK.md), and kill worker. Admin-frontend nginx proxies `/wm-api/` → worker-manager. 21 unit tests.
- **Admin auth + single entry point** (#task-d87d08bf): Nginx basic auth on admin-frontend (htpasswd generated from `ADMIN_USER`/`ADMIN_PASSWORD` env vars at container startup). Grafana proxied through `/grafana/` sub-path (no external port). Logs page embeds Grafana dashboard in iframe instead of opening new tab. Closed external ports for Grafana (3000) and API (8000) — only port 3001 exposed. `/health` excluded from auth for Docker healthcheck.
- **Admin frontend scaffold** (#task-57cc3462): React 19 + TypeScript + Vite + Tailwind CSS admin SPA in `services/admin-frontend/`. Sidebar layout with Dashboard, Projects, Tasks, Workers, Queues, Servers pages. Dashboard with live data (project count, tasks by status, queue health with 30s polling). Projects/Tasks list with filters + detail pages with event timeline. nginx multi-stage Docker build on port 3001, proxies `/api/*` → api:8000 (no CORS). Grafana iframe embedding enabled (`GF_SECURITY_ALLOW_EMBEDDING`).
- **Observability stack: Loki + Grafana + Promtail + correlation ID propagation** (#task-52743877): Added `bind_message_context()`/`unbind_message_context()` to structlog correlation module — auto-binds `correlation_id`, `task_id`, `story_id`, `project_id` from Redis stream messages. Applied to all 4 consumer patterns (base worker, PO, scaffolder, worker-manager). All 5 API clients propagate `X-Correlation-ID` header on outbound requests. Docker Compose gains Loki (log aggregation, 7-day retention), Promtail (Docker log scraper), Grafana (pre-provisioned datasource + service-logs dashboard with service/level/correlation_id filters). All services get `LOG_FORMAT` and `SERVICE_NAME` env vars. 9 new unit tests.
- **Architect specs context**: Scaffolder now parses YAML spec files (models, events, domain operations) from generated projects and saves a compact `specs_summary` to `project.config`. Architect agent sees model names, domain operations, and events when decomposing stories. New `spec_extractor.py` module in scaffolder with full test coverage.
- **Architect scaffold wait**: Architect consumer now polls `project.status` before decomposing stories. For new projects, waits up to 5 min for scaffold completion (DRAFT → ACTIVE) instead of running blind without tree/specs context.
- **Parameterized `get_project_spec` tool**: Architect can request detail levels — compact summary (default: model/event/domain names only) or full definitions (`detail="models"`, `"events"`, `"domains"`). Saves tokens by default, deep-dives only when needed.
- **PO `get_story` enriched with runs**: `get_story` tool now fetches runs for each task (id, status, type, error, timing). PO can answer "how's it going?" without needing `get_run_status` for basic info.
- **PO `story_blocked` event**: PO consumer now accepts `story_blocked` system event (previously dropped). PO prompt updated with calm messaging — "specialist is reviewing, work will resume automatically".
- **Runs API `task_id` filter**: `GET /api/runs/` now accepts `task_id` query parameter. `RunRead` schema includes `task_id` field.

### Changed
- **Architect prompt rewrite**: Removed scaffold-centric framing. Focus on "existing service with specs" rather than "scaffolded from template". Added task decomposition philosophy: slice into logical iterations, focus on boundaries between tasks, leave developer freedom for implementation decisions.
- **Developer blocker guidance**: INSTRUCTIONS.md "When You're Stuck" section rewritten. Emphasis on trying to solve problems first, but never shipping code that compromises product quality. "Better to ship nothing than ship something that works incorrectly."

## 2026-03-12

### Added
- **HITL MVP: WAITING_HUMAN_REVIEW + report-blocker + admin resume** (#task-477f5736): Developer agents can now escalate blockers instead of silently shipping workarounds. New `WAITING_HUMAN_REVIEW` status in TaskStatus and StoryStatus with full transition support. `## BLOCKED` marker in worker-wrapper (parallel to `## REJECTED`). `orch report-blocker` CLI command writes blocker reason to stdout. Engineering consumer `_handle_worker_blocked()` transitions task+story to WHR, notifies admin (Telegram, warning level), notifies user via PO (story_blocked event). `POST /tasks/{id}/resume` endpoint for admin to provide guidance and resume (WHR → IN_DEV). Task dispatcher skips WHR tasks and treats `developer_blocked` as non-retryable. Developer prompt updated with "When You're Stuck" section. ~27 new unit tests.
- **Story/Task reopen flow with user_report** (#task-ce845712): PO can now reopen completed stories instead of creating new ones, carrying a `user_report` field that describes what's wrong. New `reopen_story` PO tool calls `/api/stories/{id}/reopen` endpoint and publishes `ArchitectMessage` with `is_reopen=True` + `user_report`. Architect receives reopen context and reviews previous tasks before creating new ones. Developer sees user_report in story context (TASK.md). PO prompt updated to check `list_stories` before `create_story`. New Story model field + Alembic migration. ~20 new unit tests.

### Changed
- **ProjectStatus split: lifecycle + service_status** (#cc4d1a65): Split 13-value `ProjectStatus` enum into 3 focused enums: `ProjectStatus` (lifecycle: draft/active/paused/archived), `ServiceStatus` (runtime: not_deployed/running/degraded/down/stopped), `RepositoryStatus` (active/missing). Engineering/deploy consumers no longer touch `project.status` — only `service_status`. Alembic data migration maps all old values. All status references use enum values, no hardcoded strings. 12+ new unit tests.

### Added
- **PO bot token validation** (`validate_telegram_token` tool): PO now validates Telegram bot tokens via `getMe` API immediately after receiving them. Extracts bot username and stores both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` as project secrets. Invalid tokens fail fast at PO stage instead of wasting 30+ min on engineering + CI + deploy. PO prompt updated to use new tool instead of raw `set_project_secret` for bot tokens. 5 new unit tests.
- **Container crash logs in smoke failure output**: When smoke test fails, `SmokeTesterNode` SSHes into the deploy server and captures `docker compose logs --tail=50`. Logs are appended to the check `detail` field and flow through the existing deploy→engineering feedback loop, so the fix task receives actual tracebacks (e.g. `ModuleNotFoundError`) instead of bare "HTTP 500". Graceful fallback if SSH fails or `server_handle` is missing. 4 new unit tests.

### Fixed
- **Stale worker auto-cleanup**: `_check_project_lock()` now verifies `worker:status` — workers in terminal states (DEAD/FAILED/STOPPED) get their Redis keys cleaned up automatically, unblocking new task dispatch without manual Redis cleanup. 5 new unit tests.
- **Deploy retry limit (max 3)**: `_handle_deploy_failure()` tracks consecutive deploy attempts per story in Redis. After 3 failures, story transitions to `failed` instead of looping back to `in_progress` — prevents the infinite deploy→fail→redispatch loop that caused hundreds of failed runs and proactive message spam. 4 new unit tests.
- **Deploy deduplication: Redis lock replaces DB race** — replaced non-atomic DB-based `_check_duplicate_deploy` with atomic `SET NX` Redis lock per project. Eliminates the race window where two consumers could both pass the DB check and trigger duplicate `deploy.yml` GitHub Actions runs on the same commit. Lock held for duration of deploy, released in `finally` block. 5 new unit tests.

## 2026-03-11

### Added
- **Deploy→engineering feedback loop**: When deploy succeeds but smoke test fails, or workflow fails entirely, re-dispatch a fix task to `engineering:queue` so the developer agent can fix the code bug. Capped at 2 retry attempts via `deploy_fix_attempt` counter on both `DeployMessage` and `EngineeringMessage` contracts. 7 new unit tests.
- **PO proactive secret collection**: PO now identifies required paid API keys (OpenRouter, Stripe, etc.) from the project description and asks the user before starting engineering work.

### Fixed
- **Proactive message spam filter**: Deploy failures, smoke failures, precheck errors, and "all tasks done" messages no longer reach the user via `po:proactive`. Only two events are sent: (1) deploy success, (2) permanent story failure (user-friendly message, no technical details). Eliminates the 11+ technical spam messages seen in e2e runs.

- **Deploy auto-fallback create→feature when dir exists**: When `action=create` precheck fails with "dir already exists" (stale project.status after initial deploy), auto-switch to `action=feature` instead of failing. Eliminates the most common manual intervention from e2e runs. 4 new unit tests.

- **CI-check task fails on "no commit made"**: CI-check tasks (created_by=system) that find nothing to fix would fail with "Worker reported success but no commit was made", retry 3 times, then fail the entire story. Added `allow_no_commit` flag to `EngineeringState` — set for CI-check tasks via `_is_ci_check_task()`. Developer node returns `done` instead of `blocked` when worker succeeds without commit. Engineering consumer skips commit gate and CI gate, marks task done directly. E2E validated on fortune-teller-bot: "All 36 tests pass, CI green" → task done (previously: 3 retries → story failed). 5 new unit tests.

### Added
- **Story `deploying` status — deploy gate before completion**: Story no longer transitions to `completed` until deploy succeeds. New `DEPLOYING` status in StoryStatus enum with transitions: IN_PROGRESS → DEPLOYING → COMPLETED (on success) / IN_PROGRESS (on failure). Scheduler's `complete_stories` now transitions to `deploying` + triggers deploy with correct `action` (`feature` for already-deployed projects, `create` for new). Deploy worker completes story on success, rolls back to `in_progress` on any failure. Added `story_id` to `DeployMessage` contract, `POST /stories/{id}/deploy` endpoint, `_handle_deploy_failure` helper. 4 new transition tests.

### Fixed
- **Deploy action always `create` for already-deployed projects**: `complete_stories` now checks `project.status == ACTIVE` to send `action=feature` instead of `create`, preventing pre-check failures on update deploys.
- **Contract violations: hardcoded status strings → shared enums**: Replaced ~30 hardcoded status string literals (`"todo"`, `"done"`, `"failed"`, `"in_dev"`, `"scaffolding"`, etc.) with `TaskStatus`, `StoryStatus`, `ProjectStatus` enums from `shared/contracts/dto/` across 7 files in 4 services (scheduler, langgraph, scaffolder, api). Prevents silent breakage if enum values are renamed.
- **Contract violations: hardcoded Redis queue names → shared constants**: Removed 4 locally-defined queue name constants (`PROVISIONER_QUEUE`, `COMMAND_STREAM`, `RESPONSE_STREAM`, `WORKER_COMMANDS_STREAM`) that duplicated `shared/queues.py`. Added `WORKER_RESPONSES` constant. Replaced 5 direct `redis.xadd()` calls with `RedisStreamClient.publish_message()`/`publish()` where the abstraction was available. Updated 5 test files.

### Changed
- **CI gate: one push per story instead of per task** (#1004): CI no longer runs after every engineering task — only once at story end via the CI check task. Ordinary story tasks commit but don't push; CI check task (created_by=system) pushes and runs CI gate. Saves GitHub Actions minutes proportional to task count. Fixed `append_ci_check_task` creating CI task without `status: "todo"` (stuck in backlog forever). Extracted `_should_run_ci_gate()` and `_run_ci_gate_and_handle_failure()` helpers. Updated worker prompt to "Do NOT push unless task explicitly tells you to". 9 new tests.

## 2026-03-10

### Added
- **Live pipeline test suite** (3-tier E2E): Structured test suite split by pipeline phases — scaffold (~30s), engineering (~3.5min), full deploy (~7-10min). Module-scoped async fixtures share one pipeline run across multiple tests. Shared `pipeline_helpers.py` with all phase helpers, cleanup, and debug dump. Makefile targets: `test-live-smoke`, `test-live-engineering`, `test-live-mega`, `test-live-pipeline` (all). Auto-cleanup always runs (GitHub repos, server containers, DB records via SQL cascade, port allocations). Debug dump captures ctx + last 30 lines of docker logs on failure. Queue flush at fixture start prevents stale message pollution. 9/9 tests passing.
- **Smart CI failure triage: worker reject signal** (#task-61339aef): Workers can now signal `## REJECTED` when a CI failure is infrastructure-related (missing secrets, registry auth, Docker issues). ResultParser detects the marker, SpawnResult carries `reject_reason`, CI gate stops retries immediately. Engineering consumer transitions task to `failed` with `failure_metadata.failure_reason=worker_rejected`, story to `failed` with reject metadata, and calls `notify_admins()`. Dispatcher skips siblings of rejected tasks; supervisor skips rejected tasks from retry. CI-fix prompt template includes structured reject instructions. 27 new tests across 6 test files.

### Fixed
- **ProjectStatus enum missing "error"**: DevOps DeployerNode writes `"error"` string literal but `ProjectStatus` enum lacked `ERROR = "error"`. Scheduler's `get_projects()` → Pydantic ValidationError → crash loop → dispatcher never runs → tasks stuck at "todo". Added `ERROR = "error"` to enum.
- **Scaffolder: create GitHub repo before clone** (E2E pipeline blocker): Scaffolder tried to `git clone` a repo that didn't exist on GitHub. Added `create_repo()` call before clone (idempotent, ignores 422).
- **Scaffolder: update `git_url` after repo creation** (E2E pipeline blocker): Repository `git_url` stayed as `pending://` placeholder — CI gate couldn't find the repo. Scaffolder now updates `git_url` to real GitHub URL after creating the repo.
- **github_sync UUID serialization**: `_ingest_to_rag` passed UUID object to `json.dumps`, causing `TypeError`. Fixed with `str(project.id)`.
- **TaskCreate schema: missing `status` field** (E2E pipeline blocker): `TaskCreate` Pydantic schema didn't include `status` — Pydantic silently dropped it, SQLAlchemy used `default=backlog`. Router also hardcoded `TaskStatus.BACKLOG` in both `create_task` and `push_task`. Now accepts `status` from request body (default: backlog). Architect tasks correctly created as `todo`.
- **PO: missing Repository creation** (E2E pipeline blocker): `create_project` PO tool created Project + Story but no Repository. `scaffold_trigger` requires repository to exist (`get_repositories()` check). Added `POST /api/repositories/` call with placeholder `git_url` to `create_project` tool.
- **Scaffolder container not running** (E2E pipeline blocker): `scaffolder` service defined in docker-compose.yml but never built/started. Built and started with `docker compose up -d --build scaffolder`.

### Changed
- **Architect prompt: prefer fewer tasks**: Rewrote task creation rules to prefer fewer, larger tasks. One task per story is fine for simple projects. Only split when genuinely different concerns.
- **Makefile: `stop` is now alias for `down`**: Removed duplicated logic; `down` kills worker containers and cleans network.
- **docker-compose: scaffolder gets `GITHUB_ORG`**: Scaffolder now receives `GITHUB_ORG` env var; fixed PEM mount path typo.

### Added
- **E2E Pipeline V2 smoke test** ([report](e2e_results/pipeline_v2-20260310.md)): First full-flow test of Pipeline V2 (PO → Scaffolder → Architect → Dispatcher → Worker). Confirmed PO→Architect flow works end-to-end. Found 3 blocking bugs (all fixed), 1 medium (self-resolving after fixes). Architect decomposed "string reverser bot" into 4 chained tasks in ~42s.

## 2026-03-09

### Added
- **Scaffolder microservice**: New `services/scaffolder` service that consumes from `scaffold:queue` and prepares project repositories (copier + make setup + git push). Runs before architect so it can see the project tree. Added `ScaffoldMessage` contract, `SCAFFOLD_QUEUE`/`SCAFFOLD_GROUP` queue constants, scheduler trigger that detects draft projects with stories. Docker-compose entry with workspace + service-template volume mounts. 22 new unit tests across scaffolder, scheduler, shared, and API services.
- **Worker reuse per story** (#1002): Spawn worker container once per story, reuse for subsequent tasks (~50s saved per task). Redis hash `story:workers` maps story_id→worker_id. Engineering consumer looks up existing worker via `get_story_worker()`, passes to DeveloperNode which uses `send_task_to_worker()` (with fallback to `request_spawn()` on timeout). Scheduler cleans up worker on story complete/failure via `DeleteWorkerCommand`. Added `story_id` field to `EngineeringMessage`. Langgraph service tests added to CI matrix. 39 unit tests + 6 service tests.
- **Pipeline failure supervisor** (#1001): Three supervisor functions in the 30s dispatch loop: `supervise_stuck_stories` retries architect for stories stuck in `created` >5min (up to 3 retries); `supervise_failed_tasks` reopens failed tasks (up to `max_iterations`) or fails story with sibling cancellation; `supervise_stuck_tasks` times out `in_dev` tasks after 30min. Terminal failures notify user via PO. Added `StoryStatus.FAILED` with `/stories/{id}/fail` endpoint, `current_iteration` to `TaskUpdate` schema. 16 new tests.
- **PO tools contract tests**: 15 unit-level contract tests that import API Pydantic schemas directly and validate PO tool payloads (ProjectCreate, StoryCreate, MergeSecretsRequest). 9 integration tests that call PO tools against a real API with DB, validating full roundtrip (PO tool → HTTP → API → DB → response). New `po-tools` suite in CI integration tests matrix.

### Changed
- **Worker-manager mounts workspace by repo_id + story context** (#18): Worker-manager now mounts pre-scaffolded workspaces by `repo_id` instead of running copier+setup inside containers. Developer node passes `repo_id` instead of `ScaffoldConfig` to worker spawner. Engineering consumer builds story context (previous tasks + events) and passes it to worker via task message, giving full continuity so workers don't re-gather info each time. Extracted `_resolve_allocations()` helper. Added `get_task_events()` to langgraph API client, `story_context` field to `EngineeringState`. 11 new tests.
- **Architect: scaffolded-aware decomposition** (task-2378004c): Rewrote architect system prompt to understand scaffolded project state — creates tasks only for business logic diff, not infrastructure. Enhanced `get_project_spec` tool to surface `tree` from config and strip noisy fields (secrets, env_hints). Auto-appends CI check task after architect LLM finishes creating tasks. 15 new tests.
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
