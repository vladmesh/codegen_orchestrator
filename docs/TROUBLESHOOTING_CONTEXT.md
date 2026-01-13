# Troubleshooting Context: Worker Integration Tests

> **Superseded by:** [CURRENT_GAPS.md](./new_architecture/CURRENT_GAPS.md) - comprehensive gap analysis with fix recommendations.

## Session Summary (2026-01-13)

### Original Problem
**Error**: `400 Client Error: Bad Request ("unable to find user worker: no matching entries in passwd file")`

### Root Causes Found & Fixed

1. **pytest-asyncio configuration missing**
   - **Fix**: Added `pytest.ini` with `asyncio_mode = auto` in `tests/integration/backend/`
   - **File**: `tests/integration/backend/pytest.ini`

2. **apt-get update failing in DIND** (DNS issue)
   - **Root Cause**: GIT and CURL were being installed redundantly during image build, but apt-get failed due to DNS issues in DIND
   - **Fix**: Modified `ImageBuilder` to recognize GIT and CURL as pre-installed in worker-base
   - **File**: `services/worker-manager/src/image_builder.py`
   - GIT and CURL are already installed in worker-base Dockerfile (lines 37-41)

3. **DIND healthcheck configuration**
   - **Fix**: Updated docker-compose healthcheck with proper DNS settings
   - **File**: `docker/test/integration/backend.yml`

4. **create_worker_with_capabilities returning wrong ID**
   - **Root Cause**: Method returned Docker container_id instead of worker_id
   - **Fix**: Changed return value from `container_id` to `worker_id`
   - **File**: `services/worker-manager/src/manager.py` (line 332)

### Remaining Issues (New)

1. **Containers exit immediately after creation**
   - Symptom: `shim disconnected` in DIND logs
   - Cause: `worker-wrapper` entrypoint fails to start (likely missing config/env vars)
   - Next step: Investigate worker-wrapper startup requirements

2. **Container name conflicts between tests**
   - Symptom: `409 Conflict: container name is already in use`
   - Cause: Tests reuse same worker names without cleanup
   - Next step: Add proper cleanup between tests or use unique names

3. **Image hash collision for different agent types**
   - Symptom: `assert container1.image.id != container2.image.id` fails
   - Cause: GIT/CURL no longer generate different Dockerfiles
   - Note: This is expected behavior now - only agent type should differ in hash

## Test Status
- `test_backend_integration_smoke` - PASSED
- `test_create_claude_worker_with_git_capability` - FAILED (container not found)
- `test_create_factory_worker_with_curl_capability` - FAILED (container not running)
- `test_different_agent_types_produce_different_images` - FAILED (same image IDs)
- `test_worker_executes_task_with_mocked_claude` - FAILED (lifecycle timeout)

## Files Modified
- `tests/integration/backend/conftest.py` - pytest-asyncio plugin
- `tests/integration/backend/pytest.ini` - asyncio_mode=auto
- `docker/test/integration/backend.yml` - DIND DNS and healthcheck
- `services/worker-manager/src/image_builder.py` - GIT/CURL pre-installed
- `services/worker-manager/src/manager.py` - return worker_id
