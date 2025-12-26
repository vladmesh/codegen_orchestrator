# Refactor Audit

## DRY / duplicate logic
- [x] Duplicate workers in API and Scheduler: server sync, health checks, GitHub sync, and provisioner trigger exist twice with near-identical logic (e.g., `services/api/src/tasks/server_sync.py` and `services/scheduler/src/tasks/server_sync.py`). Decide on one home (likely scheduler) and move shared logic into a common module.
- Duplicate GitHub client implementations: `shared/clients/github.py` and `services/langgraph/src/clients/github.py` diverge in error handling and key loading. Consolidate into a single shared client.
- [x] Duplicate Time4VPS client implementations: `services/api/src/clients/time4vps.py` and `services/langgraph/src/clients/time4vps.py` overlap heavily; unify and reuse in tools/workers.

## Large / multi-responsibility modules
- `services/langgraph/src/provisioner/node.py` (~470 LOC) mixes password reset, reinstall, ansible orchestration, incident handling, and notifications. Split into phase-focused modules (access, reinstall, ansible, incidents).
- `services/langgraph/src/graph.py` (~378 LOC) mixes state schema, routing, and graph construction. Split into `state.py`, `routes.py`, and `builder.py`.
- `services/langgraph/src/nodes/product_owner.py` (~367 LOC) contains tool dispatch, response formatting, and orchestration logic. Extract formatting helpers or a response builder.
- `services/scheduler/src/tasks/server_sync.py` (~317 LOC) and `services/api/src/routers/servers.py` (~252 LOC) both carry multiple concerns; consider a service layer or helper modules.

## Config / env hygiene (policy violations)
- Many defaults are used for env vars despite the AGENTS rule forbidding defaults. Examples: `services/api/src/database.py`, `shared/logging_config.py`, `shared/redis_client.py`, `shared/notifications.py`, `services/langgraph/src/config/agent_config.py`, `services/langgraph/src/tools/base.py`, `services/langgraph/src/events.py`. Centralize config with strict validation (e.g., pydantic-settings) and fail fast on missing values.
- Scheduler already enforces `DATABASE_URL` in `services/scheduler/src/db.py` while API silently falls back to local defaults. Align behavior across services.

## Legacy / incomplete / likely broken
- [x] `services/langgraph/src/tools/time4vps.py` instantiates `Time4VPSClient()` without credentials (constructor requires username/password). either inject config or remove dead code.
- [x] `services/scheduler/src/models/` is empty but tasks import `src.models.*`; this looks broken or unfinished. Share models from the API service or move them into `shared`.
- [x] `services/api/src/tasks/*` exists even though `services/api/src/main.py` states background tasks live in scheduler. Remove or rewire to avoid confusion.

## Security-related TODOs
- API key and SSH key encryption is stubbed (e.g., `services/api/src/routers/api_keys.py`, `services/api/src/routers/servers.py`). Implement encryption or move secrets to a dedicated secrets service.

## Consistency / modernization
- Logging style is inconsistent: `services/api/src/tasks/*` uses stdlib logging; scheduler and langgraph use structlog. Standardize on structlog and ensure shared modules follow it.
- Status strings are duplicated in multiple places instead of using enums (`services/api/src/models/server.py` defines enums, but tasks compare raw strings). Use shared enums/constants to avoid drift.
- `asyncio.get_event_loop()` is used for timing in async code (e.g., `services/api/src/tasks/server_sync.py`, `services/langgraph/src/clients/time4vps.py`). Prefer `asyncio.get_running_loop()` or `time.monotonic()` in 3.12.

