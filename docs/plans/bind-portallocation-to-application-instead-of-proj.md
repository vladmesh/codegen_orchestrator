# Bind PortAllocation to Application instead of Project

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

PortAllocation currently links to Project via `project_id`, but conceptually ports belong to Application — the thing actually running on a server. Project is a higher-level abstraction ("something we work on"). Application is the runtime unit on a specific server, and it can use multiple ports (backend:8000, frontend:8001).

Current state:
- `PortAllocation.project_id` → FK to projects
- `Application.port` → single int
- Application created late (during deploy), PortAllocation created early (during resource allocation)

Target state:
- `PortAllocation.application_id` → FK to applications (remove `project_id`)
- `Application` → no `port` field, has one-to-many relationship to PortAllocation
- Application created at allocation time (not deploy time)

## Steps

1. [ ] ⚠️ needs-approval — DB migration: PortAllocation `project_id` → `application_id`, drop Application `port`
   - **Input**: `shared/models/port_allocation.py`, `shared/models/application.py`
   - **Output**: New Alembic migration; PortAllocation model has `application_id` FK (nullable for transition), `project_id` removed; Application model has no `port` field, has `port_allocations` relationship
   - **Test**: `test_application_model.py` — verify columns (no `port`), verify relationship; `test_port_allocation.py` — verify `application_id` FK exists, `project_id` gone

2. [ ] Update API schemas and router for Application (remove `port`, add `ports` list)
   - **Input**: `services/api/src/schemas/application.py`, `services/api/src/routers/applications.py`
   - **Output**: `ApplicationCreate` no `port` field; `ApplicationRead` has `ports: list[PortAllocationRead]` instead of `port: int`; `ApplicationUpdate` no `port` field; Router `create_application` doesn't accept port; list/get endpoints eagerly load port_allocations
   - **Test**: Update `test_applications_router.py` — no port in create payload, response includes `ports` list

3. [ ] Update PortAllocation API schemas and router (`project_id` → `application_id`)
   - **Input**: `services/api/src/schemas/port_allocation.py`, `services/api/src/routers/servers.py` (allocate-next), `services/api/src/routers/allocations.py`
   - **Output**: `AllocateNextPortRequest` has `application_id: int` instead of `project_id`; `PortAllocationBase/Read` has `application_id`; allocations list endpoint filters by `application_id` instead of `project_id`
   - **Test**: Update `test_port_allocation.py` — allocate-next takes application_id, allocation filter by application_id

4. [ ] Update allocator to create Application before allocating ports
   - **Input**: `services/langgraph/src/tools/allocator.py`, `services/langgraph/src/clients/api.py`, `services/langgraph/src/schemas/api_types.py`
   - **Output**: `ensure_project_allocations()` accepts `repo_id` param; creates Application via `get_or_create_application()` first; passes `application_id` to `allocate_next_port()`; returns application_id in result dict. `get_or_create_application()` no longer takes `port` param. `AllocationInfo` has `application_id` instead of `project_id`. New API client method `get_application_allocations(application_id)` replaces `get_project_allocations(project_id)`.
   - **Test**: Update `test_allocator.py` — verify Application created before allocation, application_id passed to allocate_next_port

5. [ ] Update ResourceAllocatorNode and deploy flow
   - **Input**: `services/langgraph/src/nodes/resource_allocator.py`, `services/langgraph/src/subgraphs/devops/nodes.py`
   - **Output**: `ResourceAllocatorNode.run()` passes `repo_id` to allocator; `_create_deployment_record()` no longer passes `port` to `get_or_create_application()` (app already exists); `_extract_deploy_params()` extracts port from first allocation (unchanged logic, already works)
   - **Test**: Verify deploy flow works with Application already existing from allocation step

6. [ ] Update Telegram display — show all ports per application
   - **Input**: `services/telegram_bot/src/handlers.py`
   - **Output**: If server detail view is added later, each Application shows its ports list (e.g. "fortune-teller-bot: backend:8000, frontend:8001"). For now, update any existing display that shows Application.port to use the ports list from API.
   - **Test**: Manual verification (telegram display is not unit-tested)

