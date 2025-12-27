# Service-Specific API Clients Task

## Problem
The API base URL is inconsistent across services and scripts.
We currently have a mix of:
- `API_URL` that already includes `/api` (default in `shared/config.py`), and
- code that still appends `/api/...` to that base.

This produces `.../api/api/...` and 404s (example: RAG ingest 404 from scheduler),
and the same mistake keeps reappearing in new code.

## Goal
Introduce service-specific API clients (one per service) that own the base URL
and path construction, and remove the need to manually build `/api` paths.
This also allows each service to expose only the endpoints it needs.

We should be able to remove `API_URL` from service configs and compose files
(or replace it with a clearer `API_BASE_URL` that never includes `/api`).

## Current Usage Inventory (base + path)

### Shared
- `shared/config.py` defines `API_URL` default as `http://api:8000/api`.
- `shared/notifications.py` uses `API_URL` or `API_BASE_URL` and calls `/users`.

### Telegram bot
- `services/telegram_bot/src/config.py` uses `api_url_field` (includes `/api`).
- `services/telegram_bot/src/main.py` calls `/users/upsert` and `/rag/messages`.
- `services/telegram_bot/src/handlers.py` calls `/projects` and `/servers`.

### LangGraph
- `services/langgraph/src/tools/base.py` uses `settings.api_url` as base.
- `services/langgraph/src/config/agent_config.py` uses `settings.api_url` + `/agent-configs/...`.
- `services/langgraph/src/config/cli_agent_config.py` uses `settings.api_url` + `/api/cli-agent-configs/...` (double `/api`).
- `services/langgraph/src/nodes/devops.py` uses `os.getenv("API_URL", "http://api:8000")` + `/api/service-deployments/` (double `/api` if env includes `/api`).
- `services/langgraph/src/provisioner/api_client.py` uses `os.getenv("API_URL", "http://api:8000")` + `/api/servers/...` (double `/api`).
- `services/langgraph/src/provisioner/incidents.py` uses `os.getenv("API_URL", "http://api:8000")` + `/api/incidents/...` (double `/api`).

### Scheduler
- `services/scheduler/src/config.py` uses `api_url_field` (includes `/api`).
- `services/scheduler/src/tasks/github_sync.py` previously appended `/api/rag/ingest` (fixed).

### Infra / Ansible
- `services/infrastructure/ansible/inventory/api_inventory.py` uses `ORCHESTRATOR_API_URL` + `/api/servers/` (risk of double `/api` depending on env value).

### Scripts
- `scripts/rag_ingest_public.py` uses `API_URL` + `/api/rag/ingest` (double `/api`).
- `scripts/seed_agent_configs.py` defaults to base `http://localhost:8000` + `/api/...` (works, but inconsistent).
- `scripts/test_e2e_flow.py` uses base `http://localhost:8000` + `/api/...` (works, but inconsistent).

## Proposed Approach
1. Add a minimal API client module per service, with explicit base URL handling:
   - Base must NOT include `/api`.
   - Client always prefixes `/api` internally.
   - Expose only the methods/endpoints needed by that service.

2. Replace all `os.getenv("API_URL")` usage with the service client.

3. Deprecate `API_URL` in service configs and docker-compose.
   - Prefer `API_BASE_URL` or `ORCHESTRATOR_API_URL` that never includes `/api`.
   - Provide a safe default of `http://api:8000` for docker network.

4. Update scripts to accept `--api-base-url` (no `/api`) and use a tiny helper
   to build `/api` paths consistently.

## Work Items
- [ ] Add API client in `services/langgraph/src/clients/api.py` and update:
      `config/cli_agent_config.py`, `nodes/devops.py`, `provisioner/api_client.py`,
      `provisioner/incidents.py`.
- [ ] Add API client in `services/telegram_bot/src/clients/api.py` and update:
      `main.py`, `handlers.py`.
- [ ] Add API client in `services/scheduler/src/clients/api.py` and update:
      `tasks/github_sync.py` (already switched to settings, complete the client move).
- [ ] Add API client in `services/worker-spawner` only if it starts calling API directly.
- [ ] Normalize infra and scripts:
      `services/infrastructure/ansible/inventory/api_inventory.py`,
      `scripts/rag_ingest_public.py`, `scripts/seed_agent_configs.py`,
      `scripts/test_e2e_flow.py`.
- [ ] Remove `API_URL` from `.env` and compose files; introduce `API_BASE_URL` if needed.
- [ ] Add tests for client URL joining and ensure no `/api/api` regressions.

## Acceptance Criteria
- No manual `/api` concatenation outside client modules.
- `API_URL` is no longer required for services.
- 404s from `.../api/api/...` are eliminated.
- A grep for `"/api/"` only shows client implementations and scripts that use the helper.
