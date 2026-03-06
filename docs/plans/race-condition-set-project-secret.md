# Plan: Race Condition in set_project_secret (#47)

## Context

When the LLM calls `set_project_secret` with parallel tool calls (e.g. setting `TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_ID` simultaneously), a classic read-modify-write race condition occurs:

1. Both calls GET `/api/projects/{id}` → both see the same config
2. Each decrypts secrets, adds its key, encrypts, PATCHes back
3. The last PATCH wins, overwriting the first secret

**Real incident**: `TELEGRAM_BOT_TOKEN` lost because parallel `ADMIN_TELEGRAM_ID` call overwrote it.

**Same pattern exists in**: `devops/nodes.py:_save_secrets_to_project` (line 200).

### Fix approach

Add a dedicated `POST /api/projects/{id}/config/secrets` endpoint that does atomic merge server-side using `SELECT FOR UPDATE` row locking. Callers send `{secrets: {...}, env_hints: {...}}` and the API handles decrypt→merge→encrypt→save atomically. Then simplify the PO tool and devops node to use this endpoint.

## Steps

1. [x] Add `POST /api/projects/{id}/config/secrets` endpoint (API side)
   - **Input**: `services/api/src/routers/projects.py`, `services/api/src/schemas/project.py`
   - **Output**: New endpoint that accepts `{secrets: {key: value}, env_hints: {key: hint}}`, uses `SELECT ... FOR UPDATE` to lock the project row, decrypts existing secrets, merges new ones, encrypts, saves. Returns 200 with merged secret keys (not values).
   - **Test**: Unit test — mock DB session with `FOR UPDATE`, verify merge logic (existing + new keys preserved). Test concurrent calls don't lose data.

2. [x] Simplify `set_project_secret` PO tool (langgraph side)
   - **Input**: `services/langgraph/src/po/tools.py:152-188`
   - **Output**: Tool calls `POST /api/projects/{id}/config/secrets` with `{secrets: {key: value}, env_hints: {key: hint}}` instead of GET→decrypt→merge→encrypt→PATCH. No more client-side crypto.
   - **Test**: Unit test — verify single POST call, no GET+PATCH pattern. Update existing tests in `tests/unit/po/test_tools.py` and `tests/unit/test_po_tools.py`.

3. [x] Simplify `_save_secrets_to_project` in devops nodes (langgraph side) — combined with step 4
   - **Input**: `services/langgraph/src/subgraphs/devops/nodes.py:200-234`
   - **Output**: Method calls `POST /api/projects/{id}/config/secrets` via `api_client`. No more client-side decrypt→merge→encrypt.
   - **Test**: Update existing unit tests for devops nodes to verify the new call pattern.

4. [x] Add `merge_secrets` method to `LanggraphAPIClient` — combined with step 3
   - **Input**: `services/langgraph/src/clients/api.py`
   - **Output**: `async def merge_secrets(self, project_id, secrets, env_hints=None)` that POSTs to the new endpoint. Used by both step 2 and step 3.
   - **Test**: Tested via step 3 (devops nodes call merge_secrets on the client).

5. [x] Integration test: concurrent secret writes
   - **Input**: `services/api/tests/service/` (not `tests/integration/`)
   - **Output**: Test that fires 5 parallel `POST .../config/secrets` requests with different keys and verifies all keys are present after completion.
   - **Test**: Integration test (requires DB).

## Deviations

- Steps 2, 3, 4 were committed together since they were tightly coupled
- Step 4 didn't get a separate unit test — tested transitively via step 3's devops node tests
- Integration test placed in `tests/service/` (existing structure) not `tests/integration/`
- Required adding `SECRETS_ENCRYPTION_KEY` to `docker/test/service/api.yml` for both API service and test runner containers
- Removed `encrypt_dict` import from `devops/nodes.py` (only `decrypt_dict` still needed for reading config secrets)
