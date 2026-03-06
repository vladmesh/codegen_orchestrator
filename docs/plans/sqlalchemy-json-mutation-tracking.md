# Plan: SQLAlchemy JSON Mutation Tracking — Secrets Lost on Save (#51)

## Context

`POST /projects/{id}/config/secrets` returns 200 but never persists secrets. Root cause:
`shared/models/project.py:27` uses plain `JSON` column — SQLAlchemy doesn't detect in-place
dict mutations. In `merge_secrets`, `config = project.config or {}` gets a **reference** to
the same dict object when config is not None. After mutation, `project.config = config` is
a no-op (same object identity), so SQLAlchemy sees no change and skips the UPDATE.

Secondary issue: deploy-worker doesn't reset project status on `missing_user_secrets` — the
project gets stuck in `deploying` forever.

## Steps

1. [ ] Add `MutableDict` to Project.config column ⚠️ needs-approval
   - **Input**: `shared/models/project.py`
   - **Output**: `config` column uses `MutableDict.as_mutable(JSON)` so SQLAlchemy detects in-place mutations
   - **Test**: Unit test — create a Project with config, mutate dict in-place via `project.config["key"] = "val"`, flush, re-query, assert persisted

2. [ ] Fix `merge_secrets` to copy dict before mutation
   - **Input**: `services/api/src/routers/projects.py` (line 287)
   - **Output**: `config = dict(project.config or {})` — always a new dict, so assignment triggers change detection even without MutableDict (belt-and-suspenders)
   - **Test**: Unit test — mock DB session, call `merge_secrets` with existing config, assert `project.config` is reassigned with a **new** dict object (not same identity)

3. [ ] Fix `patch_project` config assignment (belt-and-suspenders)
   - **Input**: `services/api/src/routers/projects.py` (lines 229-257)
   - **Output**: When `project_in.config` merges into existing config, ensure a new dict is assigned. Currently `project.config = project_in.config` replaces entirely (OK), but `update_project` (PUT, line 214-215) does the same — both are fine as-is since they assign a new object from the request. **No change needed** — verify with a test only.
   - **Test**: Unit test — PATCH with `config={...}`, verify config persisted (confirm current behavior is correct)

4. [ ] Deploy-worker: reset project status on `missing_user_secrets`
   - **Input**: `services/langgraph/src/workers/deploy_worker.py` (lines 340-372)
   - **Output**: After detecting `missing_user_secrets`, patch project status back to a non-stuck state (e.g. `PENDING_SECRETS` or `DRAFT`) before returning failure
   - **Test**: Unit test — mock `api_client.patch`, invoke `process_deploy_job` with a result containing `missing_user_secrets`, assert project status is rolled back

5. [ ] Integration test: secrets round-trip
   - **Input**: API + DB
   - **Output**: Integration test — POST secrets, GET project, verify config.secrets persisted and decryptable
   - **Test**: `services/api/tests/integration/test_merge_secrets.py`
