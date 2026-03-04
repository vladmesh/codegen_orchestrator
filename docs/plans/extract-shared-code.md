# Plan: Extract Shared Code — infra_client + constants (#23)

## Context

`infra_client.py` is byte-for-byte identical (279 LOC) in `services/langgraph/src/clients/` and `services/infra-service/src/clients/`. Both import `Paths` and `Timeouts` from their respective `config/constants.py`.

The constants files overlap heavily but each has service-specific additions:
- **Shared**: `Paths.SSH_KEY`, all `Timeouts` except two, `Provisioning` (identical)
- **langgraph-only**: `Timeouts.WORKER_SPAWN`, `Timeouts.PREPARER_SPAWN`, `CI` class
- **infra-service-only**: `Paths.ANSIBLE_PLAYBOOKS`, `Paths.playbook()` method

`shared/` is already mounted and installed in all services. `shared/clients/` already has `github.py`, `embedding.py`, `time4vps.py`.

**Naming constraint**: `shared/config.py` already exists (BaseSettings). Cannot create `shared/config/` directory. Will use `shared/constants.py` for the extracted constants.

## Steps

1. [x] Create `shared/constants.py` with merged constants
   - **Input**: `services/langgraph/src/config/constants.py`, `services/infra-service/src/config/constants.py`
   - **Output**: `shared/constants.py` — merged `Paths` (with `playbook()`), `Timeouts` (superset), `CI`, `Provisioning`
   - **Test**: `shared/tests/unit/test_constants.py` — verify all constant values, `Paths.playbook()` helper

2. [x] Move `infra_client.py` to `shared/clients/`
   - **Input**: `services/langgraph/src/clients/infra_client.py` (either copy — identical)
   - **Output**: `shared/clients/infra_client.py` — update import from `..config.constants` → `shared.constants`
   - **Test**: `shared/tests/unit/test_infra_client.py` — verify class instantiation, method signatures (mock asyncssh)

3. [x] Update langgraph imports
   - **Input**: 5 files in `services/langgraph/src/`:
     - `clients/infra_client.py` → delete (now in shared)
     - `config/constants.py` → keep only `CI` class, re-export rest from `shared.constants`
     - `clients/worker_spawner.py` → import `Timeouts` from `shared.constants`
     - `nodes/developer.py` → import `Timeouts` from `shared.constants`
     - `subgraphs/devops/nodes.py` → import `Paths` from `shared.constants`
     - `workers/engineering_worker.py` → import `Timeouts`, `CI` from config (CI stays local)
   - **Output**: All langgraph imports resolve; local `constants.py` is thin wrapper with `CI` only
   - **Test**: `make test-langgraph-unit` passes

4. [x] Update infra-service imports
   - **Input**: 4 files in `services/infra-service/src/`:
     - `clients/infra_client.py` → delete (now in shared)
     - `config/constants.py` → delete entirely (all constants now in shared)
     - `provisioner/ansible_runner.py` → import from `shared.constants`
     - `provisioner/node.py` → import from `shared.constants`
     - `provisioner/recovery.py` → import from `shared.constants`
   - **Output**: All infra-service imports resolve; no local constants file
   - **Test**: `make test-infra-service-unit` passes (if exists, else manual import check)

5. [x] Run full test suite and cleanup
   - **Input**: All changes from steps 1–4
   - **Output**: `make test-unit` green, `make lint` clean
   - **Test**: `make test-unit && make lint`

## Deviations

1. **Steps 3-4: Re-export instead of rewrite** — Plan suggested updating consumer imports in 9 files. Instead, made service-local `constants.py` files thin re-export wrappers (`from shared.constants import ...`). This avoids touching any consumer code while achieving the same deduplication. Both services keep their `config/constants.py` as a facade.

2. **CI class promoted to shared** — Plan said keep CI in langgraph only. Moved CI to `shared/constants.py` as well since it's a clean constant class with no service-specific dependencies. Langgraph re-exports it.

3. **Test location** — Plan specified `shared/tests/unit/test_constants.py`. Used `shared/tests/test_constants.py` and `shared/tests/clients/test_infra_client.py` to match existing test structure (no `unit/` subdirectory in shared/tests).

4. **Extra fix: ruff config** — Added `shared/tests/**` to `ruff.toml` per-file-ignores for PLR2004/S101, matching the existing `tests/**` pattern.

5. **Extra fix: shared/pyproject.toml** — Added `constants.py` to hatch `force-include` so the module is included when shared is pip-installed.
