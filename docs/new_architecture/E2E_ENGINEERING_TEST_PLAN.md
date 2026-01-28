# P4.1 — E2E Engineering Flow Test

> **Status**: In Progress (Phase 1-4.5 Done, Phase 5-7 Pending)
> **Updated**: 2026-01-18 — Phase 4.5 complete, worker ↔ mock anthropic integration working
> **Dependencies**: P3.1 (Telegram Bot), P2.2 (LangGraph)

## Goal

End-to-end test that validates the full engineering pipeline:

```
Real PO Claude → create_project → Scaffolder (real GitHub) → Mock Developer → Git commit
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         E2E Test Runner                             │
│  1. Create PO Worker (Redis)                                        │
│  2. Send task prompt to PO (Redis)                                  │
│  3. Assert project created (API)                                    │
│  4. Assert scaffolding complete (GitHub)                            │
│  5. Assert developer committed test file (GitHub)                   │
└─────────────────────────────────────────────────────────────────────┘
        │                                      │
        ▼                                      ▼
┌───────────────┐                    ┌───────────────────┐
│  Real Claude  │                    │  Mock Anthropic   │
│  (PO Worker)  │                    │  (Dev Worker)     │
│               │                    │                   │
│  ~/.claude    │                    │  Deterministic    │
│  session      │                    │  responses        │
└───────────────┘                    └───────────────────┘
```

---

## Phase 1: Mock Anthropic Server ✅ DONE

### 1.1 Create Mock Server

**Path**: `tests/e2e/mock_anthropic/`

**Tasks**:
- [x] Create `server.py` — FastAPI app responding to `/v1/messages`
- [x] Support streaming and non-streaming responses
- [x] Parse incoming prompt to determine response scenario
- [x] Return Claude API-compatible JSON

### 1.2 Docker Integration

- [x] Create `Dockerfile` for mock server (includes `curl` for healthcheck)
- [x] Add to `docker/test/e2e/e2e.yml` as `mock-anthropic` service
- [x] Expose on internal network (`http://172.30.0.40:8000`)

### 1.3 Files Created

- `tests/e2e/mock_anthropic/server.py`
- `tests/e2e/mock_anthropic/responses.py`
- `tests/e2e/mock_anthropic/Dockerfile`
- `tests/e2e/mock_anthropic/test_server.py`

---

## Phase 2: Worker ANTHROPIC_BASE_URL Support ✅ DONE

### 2.1 Verify Current Behavior

- [x] Check if `claude` CLI respects `ANTHROPIC_BASE_URL` env var — **YES** (with native installer)
- [x] Check if `WorkerConfig.env_vars` propagates to container — **YES**

> **Note**: Originally tested with deprecated npm package which had issues.
> After migrating to native bash installer (`curl -fsSL https://claude.ai/install.sh | bash`),
> `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` work correctly.

### 2.2 Changes Made

- Added `anthropic_base_url` setting to `services/langgraph/src/config/settings.py`
- Updated `request_spawn()` to pass `ANTHROPIC_BASE_URL` in worker env_vars
- Updated `docker/test/e2e/e2e.yml` to set `ANTHROPIC_BASE_URL` for langgraph service

### 2.3 Files Created

- `services/langgraph/tests/unit/test_worker_spawner.py`

---

## Phase 3: PO Worker Test Prompt ✅ DONE

### 3.1 Deterministic Prompt Builder

**Path**: `tests/e2e/e2e_prompt.py`

Creates prompts that instruct Claude PO to:
- NOT ask clarifying questions
- Immediately execute `orchestrator project create`
- Trigger engineering with `orchestrator engineering trigger`

### 3.2 Unit Tests

**Path**: `tests/e2e/test_e2e_prompt.py`

9 tests covering:
- Prompt content validation
- CLI command regex patterns

### 3.3 PO Worker E2E Test

**Path**: `tests/e2e/test_engineering_flow.py` → `TestPOWorkerFlow`

Test `test_po_creates_project_with_deterministic_prompt` (skipped, requires Claude session)

### 3.4 Verification

```bash
make test-e2e
# Result: 9 passed, 4 skipped
```

---

## Phase 4: Infrastructure Sanity Check ✅ DONE

> **Goal**: Validate GitHub + Copier + Git infrastructure works without any LLM involvement.

### 4.1 Motivation

Before testing mock LLM responses, we need to verify that the underlying infrastructure actually works:
- GitHub App authentication
- Repository creation via API
- Copier scaffolding
- Git clone/commit/push
- File verification via GitHub API

Existing tests mock these components. This phase creates a **real integration test**.

### 4.2 Test Design

**File**: `tests/e2e/test_infrastructure_sanity.py`

```
1. Create repo 'e2e-sanity-{uuid}' on GitHub (via GitHubAppClient)
2. Clone to temp directory
3. Run copier with service-template (backend module)
4. git add + commit + push
5. Verify file exists via GitHub API (e.g., Makefile)
6. Cleanup: delete test repository
```

### 4.3 Tasks

- [x] Create `tests/e2e/test_infrastructure_sanity.py`
- [x] Implement `github_client` fixture (real GitHubAppClient)
- [x] Implement `cleanup_repos` fixture (deletes test repos on teardown)
- [x] Implement `test_org` fixture (auto-detects org from GitHub App)
- [x] Add `@pytest.mark.requires_github` marker
- [x] Add skip logic if GitHub secrets not configured
- [x] Test: `test_scaffold_and_push_to_real_github`
- [x] Test: `test_github_client_authentication`
- [x] Test: `test_copier_runs_locally`
- [x] Add `delete_repo` method to `GitHubAppClient`

### 4.4 Requirements

**Environment variables** (for CI/local):
- `GITHUB_APP_ID`
- `GITHUB_APP_PRIVATE_KEY_PATH` (or `GITHUB_PRIVATE_KEY_CONTENT`)

**Org auto-detection**: Uses `get_first_org_installation()` to find target org.

**Naming convention**: `e2e-sanity-{uuid[:8]}` for test repo isolation.

### 4.5 Run Tests

```bash
# Quick run (recommended)
make test-e2e-infra

# Keep repos for debugging
E2E_KEEP_REPOS=true make test-e2e-infra

# Expected result: 3 passed in ~13s
```

### 4.6 Files Created/Modified

| File | Description |
|------|-------------|
| `tests/e2e/test_infrastructure_sanity.py` | Main test file with 3 tests |
| `.env.test` | Test environment (org: project-factory-test) |
| `secrets/github_app.pem` | GitHub App private key (gitignored) |
| `shared/clients/github.py` | Added `delete_repo()` method |
| `pyproject.toml` | Added `e2e` and `requires_github` markers |
| `Makefile` | Added `test-e2e-infra` target |
| `docker/test/e2e/Dockerfile` | Added git, curl, copier |
| `docker/test/e2e/requirements.txt` | Added copier, pyjwt[crypto] |
| `docker/test/e2e/e2e.yml` | Mount secrets, updated env vars |
| `docker-compose.yml` | Updated secrets path to `./secrets/` |

---

## Phase 4.5: Worker Mock Anthropic Integration ✅ DONE

> **Goal**: Verify that a spawned worker container actually uses mock-anthropic server and returns deterministic responses.
>
> **Status**: ✅ Complete — Tests passing
>
> **Root Cause**: Multiple issues discovered and fixed. See [PHASE_4_5_INVESTIGATION.md](./PHASE_4_5_INVESTIGATION.md).

### 4.5.1 Issues Fixed

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| Claude CLI not working | Deprecated npm package | Native installer: `curl -fsSL https://claude.ai/install.sh` |
| Worker output empty | ResultParser couldn't parse Claude CLI JSON | Added `_extract_result_text()` to handle `{"type": "result", "result": "..."}` |
| Wrong mock scenario | Server only extracted first text block | Concatenate ALL text blocks from message content |

### 4.5.2 Completed Tasks

- [x] Create `tests/e2e/test_worker_mock_anthropic.py`
- [x] Create Makefile target `test-e2e-worker-mock`
- [x] Fix response stream (`worker:responses:po` not `worker:responses:{name}`)
- [x] Add `ANTHROPIC_BASE_URL` to worker `env_vars`
- [x] Add network creation in conftest (`e2e-test-network` inside DIND)
- [x] Verify network connectivity (DIND → mock-anthropic works)
- [x] **Migrate Claude CLI to native installer** (npm deprecated)
- [x] Create `~/.claude/settings.json` for API key mode
- [x] Verify Claude CLI v2.1.12 works in container
- [x] **Fix ResultParser** for Claude CLI JSON output format
- [x] **Fix Mock server** to extract all text blocks from messages
- [x] Run E2E tests — **2 passed**

### 4.5.3 Test Results

```bash
$ make test-e2e-worker-mock

tests/e2e/test_worker_mock_anthropic.py::test_worker_receives_mock_response PASSED
tests/e2e/test_worker_mock_anthropic.py::test_worker_response_matches_scenario PASSED

==================== 2 passed in 394.85s ====================
```

### 4.5.4 Files Modified

| File | Change |
|------|--------|
| `packages/worker-wrapper/src/worker_wrapper/result_parser.py` | Handle Claude CLI JSON format |
| `packages/worker-wrapper/tests/unit/test_result_parser.py` | Add Claude CLI format tests |
| `tests/e2e/mock_anthropic/server.py` | Extract ALL text blocks from messages |
| `tests/e2e/test_worker_mock_anthropic.py` | Fix test assertions |

### 4.5.5 Key Learnings

1. **Claude CLI JSON output**: When using `--output-format json`, Claude CLI wraps response in:
   ```json
   {"type": "result", "result": "...<result>JSON</result>...", "session_id": "..."}
   ```

2. **Message content blocks**: Claude CLI sends messages with multiple text blocks:
   ```json
   {"role": "user", "content": [
     {"type": "text", "text": "<system-reminder>CLAUDE.md context...</system-reminder>"},
     {"type": "text", "text": "User's actual prompt"}
   ]}
   ```

3. **Scenario matching**: Mock server must search ALL text blocks, not just the first one.

---

## Phase 5: Developer Worker Mock Responses ✅ PARTIAL

> **Status**: Core functionality complete, developer-specific test deferred
> **Updated**: 2026-01-18

### 5.1 Completed Work

- [x] Fixed `wrapper.py` to support both `content` (PO) and `prompt` (Developer) fields
- [x] Fixed `conftest.py` with `pull=False` for DIND base image builds
- [x] Created `test_developer_mock_anthropic.py` with 2 tests
- [x] PO worker mock tests still pass (2/2)

### 5.2 Key Changes

| File | Change |
|------|--------|
| `packages/worker-wrapper/src/worker_wrapper/wrapper.py` | Support both `content` and `prompt` fields |
| `tests/e2e/conftest.py` | Add `pull=False` to prevent pulling local base images |
| `tests/e2e/test_developer_mock_anthropic.py` | New test file for developer worker |

### 5.3 Deferred: Developer-Specific Tests

Developer tests require DIND image rebuild without cache to include `wrapper.py` fix.
PO worker tests verify core functionality works.

**To run developer tests later**:
```bash
# Option 1: Clear DIND cache
docker system prune -a  # on DIND host

# Option 2: Modify conftest.py
nocache=True  # in client.images.build()
```

---

## Phase 6: Write Full E2E Test ⏳ PENDING

### 6.1 Test Structure

**File**: `tests/e2e/test_engineering_flow.py`

Existing tests (currently skipped):
- `test_engineering_creates_scaffolded_project` — Full flow test
- `test_scaffolder_receives_message` — Requires LangGraph integration
- `test_scaffolder_creates_github_repo` — Requires GitHub credentials

### 6.2 Fixtures Needed

- [ ] `github_client` — authenticated client for test org (reuse from Phase 4)
- [ ] `cleanup_repos` — delete test repos after each test (reuse from Phase 4)

---

## Phase 7: CI Integration ⏳ PENDING

### 7.1 Nightly Workflow

**File**: `.github/workflows/e2e-nightly.yml`

- [ ] Create workflow file
- [ ] Configure secrets (GH_APP_ID, GH_APP_PRIVATE_KEY, etc.)

---

## Definition of Done

| Criterion | Status |
|-----------|--------|
| Mock Anthropic server created and tested | ✅ Done |
| Worker supports `ANTHROPIC_BASE_URL` | ✅ Done |
| PO prompt module created and tested | ✅ Done |
| Infrastructure sanity test (GitHub + Copier) | ✅ Done |
| Worker actually uses mock-anthropic (Phase 4.5) | ✅ Done |
| Developer mock responses crafted | ⏳ Pending |
| E2E test passes locally with real PO Claude | ⏳ Pending |
| GitHub repo created and has test file | ⏳ Pending |
| Nightly CI workflow configured | ⏳ Pending |

---

## Appendix: Files Summary

### Created Files
| File | Description | Status |
|------|-------------|--------|
| `tests/e2e/mock_anthropic/server.py` | Mock Anthropic API | ✅ |
| `tests/e2e/mock_anthropic/Dockerfile` | Container for mock | ✅ |
| `tests/e2e/mock_anthropic/responses.py` | Response templates | ✅ |
| `tests/e2e/mock_anthropic/test_server.py` | Unit tests | ✅ |
| `tests/e2e/e2e_prompt.py` | Deterministic prompt builder | ✅ |
| `tests/e2e/test_e2e_prompt.py` | Unit tests for prompt | ✅ |
| `tests/e2e/test_engineering_flow.py` | E2E tests (partial) | ✅ |
| `tests/e2e/test_infrastructure_sanity.py` | GitHub + Copier sanity test | ✅ |
| `tests/e2e/test_worker_mock_anthropic.py` | Worker ↔ Mock Anthropic integration | ✅ |
| `docs/new_architecture/PHASE_4_5_INVESTIGATION.md` | Investigation report | ✅ |
| `services/langgraph/tests/unit/test_worker_spawner.py` | URL propagation test | ✅ |
| `packages/worker-wrapper/tests/unit/test_result_parser.py` | Claude CLI JSON parsing tests | ✅ |
| `.github/workflows/e2e-nightly.yml` | Nightly CI | ⏳ |

### Modified Files
| File | Change | Status |
|------|--------|--------|
| `docker/test/e2e/e2e.yml` | Add mock-anthropic, mount secrets, env vars | ✅ |
| `docker/test/e2e/requirements.txt` | Add fastapi, uvicorn, copier, pyjwt | ✅ |
| `docker/test/e2e/Dockerfile` | Add git, curl for copier/healthcheck | ✅ |
| `docker-compose.yml` | Update secrets path to `./secrets/` | ✅ |
| `Makefile` | Add `test-e2e-infra`, `test-e2e-worker-mock` targets | ✅ |
| `services/langgraph/src/config/settings.py` | Add anthropic_base_url | ✅ |
| `services/langgraph/src/clients/worker_spawner.py` | Pass ANTHROPIC_BASE_URL | ✅ |
| `shared/clients/github.py` | Add `delete_repo()` method | ✅ |
| `pyproject.toml` | Add `e2e`, `requires_github` markers | ✅ |
| `packages/worker-wrapper/src/worker_wrapper/result_parser.py` | Handle Claude CLI JSON output | ✅ |
| `services/worker-manager/images/worker-base-claude/Dockerfile` | Native Claude CLI installer | ✅ |
| `services/universal-worker/Dockerfile` | Remove npm, nodejs | ✅ |

### Config Files (not in git)
| File | Description |
|------|-------------|
| `.env.test` | Test org config (project-factory-test) |
| `secrets/github_app.pem` | GitHub App private key |
