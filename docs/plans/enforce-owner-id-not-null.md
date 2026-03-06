# Plan: Enforce Project-User Binding (owner_id NOT NULL) (#39)

## Context

Currently `owner_id` on the `projects` table is nullable. Projects can be created without an owner:
- API allows `POST /api/projects/` without `X-Telegram-ID` header → `owner_id=None`
- `github_sync` discovers repos and creates projects via API without any user context → orphan projects

This creates data integrity issues: unowned projects can't be notified, access-checked properly, or attributed to users.

**Goal**: Make `owner_id` NOT NULL. API rejects project creation without a valid user. `github_sync` stops creating orphan projects — sends admin warning instead.

### Current state
- **Model**: `shared/models/project.py:33` — `owner_id: Mapped[int | None]`, nullable FK
- **DTO**: `shared/contracts/dto/project.py:69,85` — `owner_id: int | None = None` in both `ProjectUpdate` and `ProjectDTO`
- **API schemas**: `services/api/src/schemas/project.py` — no `owner_id` at all (not in `ProjectCreate`, `ProjectRead`, `ProjectUpdate`)
- **API create**: `services/api/src/routers/projects.py:84-93` — owner_id optional
- **github_sync**: `services/scheduler/src/tasks/github_sync.py:213-227` — creates `ProjectCreate` without owner
- **Webhook**: `services/api/src/routers/webhooks.py:113` — `if project.owner_id` guard before notification
- **LangGraph**: `services/langgraph/src/schemas/api_types.py:59` — `owner_id: int | None`

## Steps

1. [ ] ⚠️ needs-approval — Migration: `owner_id` NOT NULL
   - **Input**: `shared/models/project.py`, existing migration `2df4b21abbbe`
   - **Output**:
     - New Alembic migration: backfill orphan projects to admin user (or delete them), then `ALTER COLUMN owner_id SET NOT NULL`
     - Model updated: `owner_id: Mapped[int]` (not Optional)
   - **Test**: Migration unit test — verify upgrade/downgrade SQL is correct (or manual `make migrate` check)

2. [ ] ⚠️ needs-approval — DTO & API schema changes
   - **Input**: `shared/contracts/dto/project.py`, `services/api/src/schemas/project.py`
   - **Output**:
     - `ProjectDTO.owner_id: int` (required, not Optional)
     - `ProjectUpdate.owner_id` removed (owner shouldn't change after creation)
     - API `ProjectRead` schema: add `owner_id: int` field
     - LangGraph `api_types.py`: `owner_id: int` (not Optional)
   - **Test**: Unit test — `ProjectDTO` validation fails without `owner_id`; `ProjectRead` includes `owner_id`

3. [ ] API: Require `X-Telegram-ID` on project creation
   - **Input**: `services/api/src/routers/projects.py`
   - **Output**:
     - `POST /api/projects/` returns 400 if `X-Telegram-ID` header is missing
     - Remove the "optional owner" code path (lines 84-93)
   - **Test**: Update `test_create_project.py`:
     - `test_create_project_without_header_returns_400` (was 201 with None owner)
     - `test_create_project_with_header_sets_owner` (unchanged)
     - `test_create_project_unknown_user_returns_404` (unchanged)

4. [ ] github_sync: Stop creating orphan projects
   - **Input**: `services/scheduler/src/tasks/github_sync.py`
   - **Output**:
     - `_sync_single_repo`: when project not found in DB → call `notify_admins` with warning instead of `api_client.create_project`
     - Doc sync (`_sync_project_docs`) still runs for existing projects
   - **Test**: Update `test_github_sync.py`:
     - `test_sync_single_repo_creates_new_project` → rename to `test_sync_single_repo_notifies_admins_for_unknown_repo`
     - Verify `notify_admins` called, `create_project` NOT called

5. [ ] Webhook: Remove `if project.owner_id` guard
   - **Input**: `services/api/src/routers/webhooks.py:110-118`
   - **Output**: Remove the `if project.owner_id:` conditional — owner always exists now, query User directly
   - **Test**: Update `test_webhooks.py` if it tests the no-owner path

6. [ ] Integration test: project creation with ownership
   - **Input**: All changed components
   - **Output**: Service-level integration test verifying:
     - POST without header → 400
     - POST with valid header → 201 + owner_id set
     - GET returns owner_id in response
   - **Test**: `services/api/tests/service/test_project_ownership_integration.py`
