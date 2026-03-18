# Удалить orchestrator-cli, перевести агента на curl к localhost:9090

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Orchestrator-cli is a Python package (`packages/orchestrator-cli/`) installed in worker containers that gives agents CLI commands (`orchestrator project`, `orchestrator deploy`, `orchestrator dev-env`, `orchestrator respond`, `orch report-blocker`). It imports from `shared/` — causing namespace conflicts when generated projects also have a `shared/` directory.

The HTTP result server (localhost:9090) was added to worker-wrapper in the previous task (#task-7397ff9b). It already handles `/complete`, `/failed`, `/blocker` endpoints. The agent can now report results via curl instead of orchestrator-cli.

**What needs to change:**
- Agent instructions: replace all `orchestrator` CLI references with curl to localhost:9090
- Dev-env commands (`start-infra`, `stop-infra`, `reset-infra`, `compose`): these call worker-manager HTTP API via httpx — replace with direct curl from agent to worker-manager (URL provided via env var)
- Remove orchestrator-cli package, its Dockerfile references, pyproject.toml entries, CI config, test scripts
- Remove `shared/schemas/tool_groups.py` and `shared/schemas/tool_registry.py` (only exist for orchestrator-cli doc generation, never called by services)
- Clean up env vars in container_config.py

## Steps

1. [ ] Update INSTRUCTIONS.md — replace CLI commands with curl
   - **Input**: `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`
   - **Output**: All `orchestrator` and `orch` commands replaced with curl equivalents:
     - Result reporting → `curl localhost:9090/complete`, `/failed`, `/blocker`
     - Reject (infra issue) → `curl localhost:9090/failed` with reason
     - Report blocker → `curl localhost:9090/blocker` with reason
     - Dev-env infra → `curl $WORKER_MANAGER_URL/api/worker/$WORKER_ID/infra/compose` with JSON payload
     - Remove `orchestrator respond` references (respond capability not available via HTTP yet — out of scope)
     - Remove "Orchestrator CLI Reference" appendix concept (tool_groups.py generated it)
   - **Test**: Unit test that INSTRUCTIONS.md loads correctly and contains expected curl patterns

2. [ ] Remove orchestrator-cli from worker-base-common Dockerfile
   - **Input**: `services/worker-manager/images/worker-base-common/Dockerfile`
   - **Output**: Remove COPY and wheel build lines for orchestrator-cli (lines 20, 23-24, 31, 33). Keep worker-wrapper build intact. The dependency collection script merges both pyproject.toml files — simplify to only worker-wrapper.
   - **Test**: `docker build` succeeds (verified in step 7)

3. [ ] Remove ORCHESTRATOR_* env vars from container_config.py, keep WORKER_MANAGER_URL
   - **Input**: `services/worker-manager/src/container_config.py`
   - **Output**: Remove `ORCHESTRATOR_API_URL`, `ORCHESTRATOR_REDIS_URL` (only used by CLI). Rename `ORCHESTRATOR_WORKER_MANAGER_URL` → `WORKER_MANAGER_URL` (agent needs it for dev-env curl). Remove the comment about orchestrator CLI.
   - **Test**: Unit test for `to_env_vars()` output — no ORCHESTRATOR_ keys, WORKER_MANAGER_URL present

4. [ ] Delete `packages/orchestrator-cli/` directory
   - **Input**: `packages/orchestrator-cli/` (entire directory)
   - **Output**: Directory deleted
   - **Test**: Directory does not exist

5. [ ] Remove shared/schemas/tool_registry.py and tool_groups.py
   - **Input**: `shared/schemas/tool_registry.py`, `shared/schemas/tool_groups.py`, `shared/schemas/__init__.py`
   - **Output**: Delete tool_registry.py and tool_groups.py. Remove their exports from `shared/schemas/__init__.py`. Delete `shared/tests/test_tool_registry.py`.
   - **Test**: `from shared.schemas import ...` still works for remaining exports

6. [ ] Clean up root pyproject.toml, Makefile, CI, test scripts
   - **Input**:
     - `pyproject.toml` — remove orchestrator-cli from dev deps, workspace members, dependency overrides
     - `Makefile` — remove `packages/orchestrator-cli` from WORKER_SOURCE_HASH, update comments
     - `.github/workflows/ci.yml` — remove cli test suite and trigger paths
     - `scripts/test-unit-local.sh` — remove orchestrator-cli test suite entry
   - **Output**: No references to orchestrator-cli in build/CI/test infrastructure
   - **Test**: `make lint` passes, `grep -r orchestrator.cli` finds only docs/brainstorms (expected)

7. [ ] Update and run tests
   - **Input**: All modified files
   - **Output**:
     - `services/worker-manager/tests/` — update container_config tests for new env var names
     - `services/langgraph/tests/unit/test_worker_spawner_*.py` — no changes needed (mock load_developer_instructions)
     - Remove `packages/orchestrator-cli/tests/` (deleted with package)
     - Remove `shared/tests/test_tool_registry.py` (deleted with module)
   - **Test**: `make test-unit` passes, `make lint` passes

8. [ ] Update docs (non-code references)
   - **Input**: `docs/CONTRACTS.md`, `docs/NODES.md`, `docs/PIPELINE_V2.md`, `docs/GLOSSARY.md`
   - **Output**: Replace `orchestrator` CLI references with curl equivalents or generic descriptions. Update architecture diagrams if they mention orchestrator-cli.
   - **Test**: No stale `orchestrator-cli` references in docs (except brainstorms which are historical)

