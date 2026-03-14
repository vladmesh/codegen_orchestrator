# Introduce Application entity and refactor Deployment model

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

ServiceDeployment currently serves as both deployment log and runtime state, creating duplicates (21 records for 3 actual services on vps-267180). Root cause: each deploy creates a new record with status="running", old records are never closed.

Decision (discussed with user): clean separation into three concepts:
- **Application** (new) — runtime entity, "what is deployed where". Links repo + server.
- **Deployment** (renamed from ServiceDeployment) — immutable log of deploy attempts.
- **Project.service_status** — to be derived from Application statuses (deprecate direct writes).

Current code references ServiceDeployment in ~17 files across shared/, api/, langgraph/, admin-frontend/.

## Steps

1. [ ] Create Application model + ApplicationStatus enum + migration ⚠️ needs-approval
   - **Input**: `shared/models/`, `shared/contracts/dto/`, `services/api/migrations/`
   - **Output**: New `shared/models/application.py` with: id (int PK), repo_id (FK → repositories.id), server_handle (FK → servers.handle), status (ApplicationStatus: not_deployed/running/stopped/down/degraded), port (int), service_name (str), last_health_check (datetime|null), unique constraint (repo_id, server_handle). New `shared/contracts/dto/application.py` with ApplicationStatus enum. Migration creates `applications` table.
   - **Test**: Unit test — model instantiation, enum values, unique constraint

2. [ ] Create DeploymentResult enum, refactor ServiceDeployment → Deployment model ⚠️ needs-approval
   - **Input**: `shared/models/service_deployment.py`, `shared/models/__init__.py`, `shared/contracts/dto/`
   - **Output**: Rename class ServiceDeployment → Deployment (keep tablename `service_deployments`). New DeploymentResult enum (pending/success/failed/canceled) replaces DeploymentStatus. Add `application_id` FK → applications.id (nullable initially for migration). Keep `project_id`, `server_handle` as denormalized fields for now. Rename `status` column → `result` in migration.
   - **Test**: Unit test — model instantiation, DeploymentResult enum values

3. [ ] Data migration: backfill Applications from existing ServiceDeployment records ⚠️ needs-approval
   - **Input**: Existing `service_deployments` rows, `repositories` table
   - **Output**: Alembic migration that: (a) creates Application for each unique (project_id, server_handle, service_name) combo in service_deployments, linking to primary repo; (b) sets application_id on existing deployments; (c) marks only the latest deployment per application as result=success, rest as result=success too (they were all successful deploys); (d) sets Application.status = running for apps with recent deploys
   - **Test**: Migration up/down tested manually

4. [ ] API: Application CRUD endpoints + update Deployment schemas
   - **Input**: `services/api/src/routers/`, `services/api/src/schemas/`
   - **Output**: New `routers/applications.py` with: GET /applications/ (list, filter by server_handle/project_id/status), GET /applications/{id}, POST /applications/, PATCH /applications/{id} (status updates for health checker). Update `schemas/service_deployment.py`: rename ServiceDeployment* → Deployment*, replace status→result field. Update `routers/service_deployments.py` and `routers/servers.py` imports. Register new router in main.py. GET /servers/{handle}/services now queries Applications instead of Deployments.
   - **Test**: Unit tests for Application CRUD, updated Deployment schema tests

5. [ ] Update DeployerNode to create/update Application on deploy
   - **Input**: `services/langgraph/src/subgraphs/devops/nodes.py`, `services/langgraph/src/clients/api.py`
   - **Output**: DeployerNode._create_deployment_record() renamed, now: (a) GET or POST Application (upsert by repo_id + server_handle); (b) POST Deployment with result=pending, update to success/failed on completion; (c) PATCH Application.status = running on success. API client gets new methods: get_or_create_application(), create_deployment(), update_deployment_result(), update_application_status().
   - **Test**: Unit test — mock API calls, verify upsert logic and result transitions

6. [ ] Update deploy consumer service_status handling
   - **Input**: `services/langgraph/src/consumers/deploy.py`
   - **Output**: On deploy success/failure, update Application.status via API instead of (or in addition to) Project.service_status. Keep Project.service_status writes for backward compatibility until fully deprecated.
   - **Test**: Unit test — verify Application.status updated on success/failure/smoke-failure

7. [ ] Update admin frontend: ServersPage shows Applications
   - **Input**: `services/admin-frontend/src/types/api.ts`, `services/admin-frontend/src/pages/ServersPage.tsx`
   - **Output**: New Application type in api.ts. ServersPage expanded rows show Applications (with status, port, last_health_check) instead of raw Deployments. Each Application links to project and shows deployment count.
   - **Test**: Build passes (npx tsc --noEmit + vite build)

8. [ ] Cleanup: update tools schemas, tests, live test cleanup scripts
   - **Input**: `services/langgraph/src/schemas/tools.py`, `services/langgraph/src/tools/servers.py`, `services/langgraph/tests/unit/test_deployer.py`, `tests/live/`, `scripts/clean_live_tests.py`
   - **Output**: All references to ServiceDeployment updated. Tool schemas use Deployment. Tests pass. Live test cleanup handles both tables.
   - **Test**: make test-unit passes, make lint passes

