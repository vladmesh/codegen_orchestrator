# Plan: Port Allocation Locking (#31)

## Context

Two parallel deploys can read the same port list, both pick the same "next available" port, and both attempt to allocate it. The second one fails with a 400 error, but there's no retry — the deploy just fails.

**Root cause**: Classic TOCTOU (time-of-check-time-of-use) race in `tools/allocator.py:154-162`. `_get_next_available_port` reads ports via GET, then `_allocate_port` writes via POST. No atomicity between read and write.

**Current state**:
- `PortAllocation` model has no unique constraint on `(server_handle, port)` — DB allows duplicates
- API endpoint `servers.py:148-154` checks for existing allocation before INSERT, but this is also a TOCTOU within the same request (no row-level lock)
- `ensure_project_allocations` is called from both `ResourceAllocatorNode` (engineering flow) and `deploy_worker` (deploy flow)

**Fix approach**: DB-level unique constraint + atomic allocate-or-retry in the API endpoint.

## Steps

1. [ ] Add unique constraint on `(server_handle, port)` via Alembic migration
   - **Input**: `shared/models/port_allocation.py`, new migration file
   - **Output**: `UniqueConstraint("server_handle", "port")` on `PortAllocation` model; Alembic migration that adds the constraint
   - **Test**: Unit test verifying model has the constraint defined. Integration test: inserting duplicate `(server_handle, port)` raises `IntegrityError`

2. [ ] Add atomic allocate-or-next endpoint to API
   - **Input**: `services/api/src/routers/servers.py` (the `allocate_port` endpoint)
   - **Output**: New `POST /{handle}/ports/allocate-next` endpoint that atomically finds next free port and allocates it. Uses `SELECT ... FOR UPDATE` or catches `IntegrityError` + retry loop. Returns allocated port. Existing `POST /{handle}/ports` stays for explicit port allocation (now protected by unique constraint)
   - **Test**: Unit test for the new endpoint. Integration test: two concurrent allocations get different ports (asyncio.gather)

3. [ ] Add API client method and update allocator to use atomic endpoint
   - **Input**: `services/langgraph/src/clients/api.py`, `services/langgraph/src/tools/allocator.py`
   - **Output**: New `api_client.allocate_next_port(server_handle, service_name, project_id)` method. `ensure_project_allocations` calls the new atomic endpoint instead of separate `_get_next_available_port` + `_allocate_port`. Remove `_get_next_available_port` and `_allocate_port` private functions
   - **Test**: Unit test for `ensure_project_allocations` with mocked API client verifying single atomic call per module

4. [ ] Update PO tool `get_next_available_port` to use atomic endpoint
   - **Input**: `services/langgraph/src/tools/ports.py`
   - **Output**: `get_next_available_port` tool removed (replaced by atomic allocation). `allocate_port` tool updated to catch unique constraint 400 errors and return clear message. If `ports.py` tools are unused by PO (only `allocator.py` is used in practice), remove the file entirely
   - **Test**: Verify tools register correctly, test error handling for duplicate port

5. [ ] Integration test: concurrent allocation race
   - **Input**: Test infrastructure
   - **Output**: Integration test in `tests/integration/backend/` that fires two concurrent `POST /{handle}/ports/allocate-next` requests and asserts both succeed with different ports
   - **Test**: The test itself validates the race condition fix
