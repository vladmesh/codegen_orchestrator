# PO tools contract tests — validate payloads against API schemas

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

PO unit tests mock the httpx.AsyncClient, so payloads are never validated against actual Pydantic schemas. Example: `project_id="abc"` passes tests but would fail at runtime because `StoryCreate.project_id` expects `uuid.UUID`. The task description mentions two approaches — contract tests (validate payloads in-process) and service-level tests (call real API). We do both: unit-level contract tests (fast, no infra) + service-level integration tests (PO tools → real API → DB).

**Key schema mismatches to catch:**
- `ProjectCreate.id` expects `uuid.UUID`, tools send `str(uuid.uuid4())` — works, but mock tests don't validate
- `StoryCreate.project_id` expects `uuid.UUID` — tools pass string
- `MergeSecretsRequest` — validated but never contract-tested
- `StoryCreate.type` expects `Literal[StoryType.PRODUCT, StoryType.TECHNICAL]` — tools pass `StoryType.PRODUCT.value` (string "product")

**Current state:**
- Unit tests: `services/langgraph/tests/unit/po/test_tools.py` — 30+ tests, all mocked
- Service tests dir: `services/langgraph/tests/service/` — exists but empty (only `__init__.py`)
- Service compose: `docker/test/service/langgraph.yml` — uses MockServer for API (not real API)
- API service compose: `docker/test/service/api.yml` — real API + DB + Redis

## Steps

1. [ ] Add contract validation tests (unit-level, no infra)
   - **Input**: `services/api/src/schemas/{project,story,run}.py`, `services/langgraph/src/agents/po/tools.py`
   - **Output**: `services/langgraph/tests/unit/po/test_tool_contracts.py` — imports API Pydantic schemas directly, builds the same payloads PO tools build, validates them with `Schema.model_validate(payload)`. Covers: `ProjectCreate`, `StoryCreate`, `MergeSecretsRequest`. Tests both valid payloads and edge cases (non-UUID project_id, invalid enum values).
   - **Test**: `uv run pytest services/langgraph/tests/unit/po/test_tool_contracts.py -v`

2. [ ] Update service test compose to use real API instead of MockServer
   - **Input**: `docker/test/service/langgraph.yml`, `docker/test/service/api.yml`
   - **Output**: Updated `docker/test/service/langgraph.yml` — replace MockServer with real API service (api + db + redis), so langgraph service tests can hit a real API with DB. Keep Redis shared.
   - **Test**: `make test-service SERVICE=langgraph` starts without errors

3. [ ] Add service-level integration tests for PO tools
   - **Input**: `services/langgraph/tests/service/`, PO tools, real API
   - **Output**: `services/langgraph/tests/service/test_po_tools.py` — calls PO tools with `init_po_clients(httpx.AsyncClient(base_url=API_URL))` against real API. Tests: `create_project` → project exists in DB, `create_story` → story exists + architect message published, `set_project_secret` → secrets stored, `list_projects/get_project` → returns data. Validates full roundtrip: tool → HTTP → API → DB → response.
   - **Test**: `make test-service SERVICE=langgraph`

4. [ ] Add conftest for service tests
   - **Input**: `services/langgraph/tests/service/`
   - **Output**: `services/langgraph/tests/service/conftest.py` — fixtures: `api_client` (httpx.AsyncClient pointed at real API), `stream_client` (RedisStreamClient), `po_tools_init` (calls `init_po_clients`), test user creation via API, cleanup between tests.
   - **Test**: fixtures load without errors in test_po_tools.py

5. [ ] Verify and document
   - **Input**: all new test files
   - **Output**: All tests pass: `uv run pytest tests/unit/po/test_tool_contracts.py` (contract) + `make test-service SERVICE=langgraph` (integration). CHANGELOG entry.
   - **Test**: both test commands green

