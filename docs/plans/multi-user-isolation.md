# Plan: Multi-user Isolation Fix (#30)

## Context

PO tools call the API without `X-Telegram-ID` header, so:
- `create_project` creates projects with `owner_id = NULL` (admin-only by default)
- `list_projects` returns ALL projects to all users (no filtering)
- `get_project` / `set_project_secret` access any project without ownership check

Workers (`engineering_worker`, `deploy_worker`) also call the API without headers —
the API's `_check_project_access()` skips auth when `telegram_id is None` (backward compat).

Source: brainstorm `docs/brainstorms/epic-decomposition.md`, E2E analysis.

### Current state

- **PO tools** (`services/langgraph/src/po/tools.py`): use raw `httpx.AsyncClient` (module-level `_api_client`), no headers. `user_id` is available in `config["configurable"]["user_id"]` but unused for API calls.
- **API projects.py** (lines 30-55): `_check_project_access` returns immediately if `telegram_id is None`. `list_projects` returns all if no header. `create_project` sets `owner_id=None` if no header.
- **API tasks.py** (lines 30-55): Same pattern — `_check_task_access` skips if no header.
- **LanggraphAPIClient** (`services/langgraph/src/clients/api.py`): low-level methods (`get`, `post`, etc.) already accept `headers` kwarg. High-level methods (`get_project`, `list_projects`) don't pass headers.
- **Workers**: have `user_id` from queue message but don't pass it to API client.

### Strategy

Thread `user_id` through PO tools → httpx headers, and through workers → LanggraphAPIClient → headers. The API already enforces ownership when the header is present — we just need to send it.

**Important**: Workers are internal services that need to operate on projects. They should pass `X-Telegram-ID` for ownership validation, but the API must still allow system calls (workers acting on behalf of users). Current design is sufficient: workers pass the user's telegram_id from the queue message.

## Steps

1. [ ] PO tools: pass `X-Telegram-ID` header in API calls
   - **Input**: `services/langgraph/src/po/tools.py`, `services/langgraph/src/po/consumer.py`
   - **Output**: All PO tool functions that call the API include `X-Telegram-ID` header from `config["configurable"]["user_id"]`. Specifically:
     - `create_project` — add `config: RunnableConfig` param, pass header
     - `list_projects` — add `config: RunnableConfig` param, pass header
     - `get_project` — add `config: RunnableConfig` param, pass header
     - `set_project_secret` — add `config: RunnableConfig` param, pass header
     - `get_task_status` — add `config: RunnableConfig` param, pass header
     - Extract helper: `_user_headers(config)` → `{"X-Telegram-ID": user_id}`
   - **Test**: Unit tests in `services/langgraph/tests/unit/po/test_tools.py` — verify each tool passes `X-Telegram-ID` header when calling API. Verify `create_project` sends header so `owner_id` gets set.

2. [ ] API: enforce `X-Telegram-ID` requirement for project creation
   - **Input**: `services/api/src/routers/projects.py`
   - **Output**: `create_project` endpoint requires `X-Telegram-ID` (return 400 if missing and project would get `owner_id=None`). This prevents orphan projects.
   - **Test**: Unit test — POST `/api/projects/` without header returns 400. With valid header returns 201 and `owner_id` is set.

3. [ ] LanggraphAPIClient: add `user_id` support to high-level methods
   - **Input**: `services/langgraph/src/clients/api.py`
   - **Output**: `get_project()` and `list_projects()` accept optional `telegram_id: int | None` param. When provided, pass `{"X-Telegram-ID": str(telegram_id)}` header. Other methods used by workers that touch user-scoped resources also get the param.
   - **Test**: Unit tests for `LanggraphAPIClient.get_project(id, telegram_id=123)` — verify header passed to httpx.

4. [ ] Workers: pass `user_id` as `X-Telegram-ID` in API calls
   - **Input**: `services/langgraph/src/workers/engineering_worker.py`, `services/langgraph/src/workers/deploy_worker.py`
   - **Output**: Both workers extract `user_id` from queue message, resolve `telegram_id` (user_id in queue IS the telegram_id string), pass it to `api_client.get_project(project_id, telegram_id=...)`. If `user_id` is empty/missing, worker logs warning and continues (graceful degradation for webhook-triggered deploys where user_id may be "").
   - **Test**: Unit tests — worker calls `get_project` with telegram_id from message. Worker with empty user_id still processes (no crash).

5. [ ] Integration test: multi-user isolation end-to-end
   - **Input**: All modified files
   - **Output**: Integration test that:
     - Creates two users (telegram_id 111, 222)
     - User 111 creates project A (via API with header)
     - User 222 creates project B
     - User 111 lists projects → sees only A
     - User 222 lists projects → sees only B
     - User 222 tries GET /api/projects/{A.id} → 403
     - No header: list projects → still returns all (system compat)
   - **Test**: `services/api/tests/integration/test_user_isolation.py`

## Notes

- `user_id` in queue messages is the telegram_id as string (set by PO consumer from `update.effective_user.id`)
- Webhook-triggered deploys (`triggered_by=WEBHOOK`) get telegram_id from `project.owner_id → User.telegram_id` (webhooks.py:147-152), so they have valid user_id
- Allocations router is admin-only — no changes needed
- Task update is system-only — no changes needed
