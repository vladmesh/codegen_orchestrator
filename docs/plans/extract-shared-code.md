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

1. [ ] Create `shared/constants.py` with merged constants
   - **Input**: `services/langgraph/src/config/constants.py`, `services/infra-service/src/config/constants.py`
   - **Output**: `shared/constants.py` — merged `Paths` (with `playbook()`), `Timeouts` (superset), `CI`, `Provisioning`
   - **Test**: `shared/tests/unit/test_constants.py` — verify all constant values, `Paths.playbook()` helper

2. [ ] Move `infra_client.py` to `shared/clients/`
   - **Input**: `services/langgraph/src/clients/infra_client.py` (either copy — identical)
   - **Output**: `shared/clients/infra_client.py` — update import from `..config.constants` → `shared.constants`
   - **Test**: `shared/tests/unit/test_infra_client.py` — verify class instantiation, method signatures (mock asyncssh)

3. [ ] Update langgraph imports
   - **Input**: 5 files in `services/langgraph/src/`:
     - `clients/infra_client.py` → delete (now in shared)
     - `config/constants.py` → keep only `CI` class, re-export rest from `shared.constants`
     - `clients/worker_spawner.py` → import `Timeouts` from `shared.constants`
     - `nodes/developer.py` → import `Timeouts` from `shared.constants`
     - `subgraphs/devops/nodes.py` → import `Paths` from `shared.constants`
     - `workers/engineering_worker.py` → import `Timeouts`, `CI` from config (CI stays local)
   - **Output**: All langgraph imports resolve; local `constants.py` is thin wrapper with `CI` only
   - **Test**: `make test-langgraph-unit` passes

4. [ ] Update infra-service imports
   - **Input**: 4 files in `services/infra-service/src/`:
     - `clients/infra_client.py` → delete (now in shared)
     - `config/constants.py` → delete entirely (all constants now in shared)
     - `provisioner/ansible_runner.py` → import from `shared.constants`
     - `provisioner/node.py` → import from `shared.constants`
     - `provisioner/recovery.py` → import from `shared.constants`
   - **Output**: All infra-service imports resolve; no local constants file
   - **Test**: `make test-infra-service-unit` passes (if exists, else manual import check)

5. [ ] Run full test suite and cleanup
   - **Input**: All changes from steps 1–4
   - **Output**: `make test-unit` green, `make lint` clean
   - **Test**: `make test-unit && make lint`
