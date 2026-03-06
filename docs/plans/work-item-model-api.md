# Plan: WorkItem Model + API + Backlog Migration (#55)

## Context

Переход от "проект = один процесс" к системе управления задачами. Сейчас описание фичи теряется в Redis queue, нет истории итераций, нет бэклога в БД. Brainstorm: [orchestrator-v2-task-management.md](../brainstorms/orchestrator-v2-task-management.md).

Это Step 0 — фундамент. Новые таблицы, CRUD API, action-based status transitions. Ничего не ломает, существующий код не трогаем. Q4 resolved: `type=create` включён (create_project тоже станет WorkItem).

### Что есть
- `Task` модель — runtime execution (queued/running/completed/failed)
- `Project` модель — проект с config, status, owner_id
- API: REST CRUD для projects и tasks (`services/api/src/routers/`)
- Alembic миграции в `services/api/migrations/versions/`

### Что добавляем
- `WorkItem` модель — единица работы с agile-статусами (backlog → todo → in_dev → ... → done)
- `WorkItemEvent` модель — история переходов и итераций
- `Task.work_item_id` + `Task.iteration` — связь execution → planning layer
- API: CRUD + action endpoints для work items
- Скрипт миграции backlog.md Queue → work_items в БД

## Steps

1. [ ] WorkItem + WorkItemEvent SQLAlchemy models
   - **Input**: `shared/models/base.py`, `shared/models/task.py` (pattern reference)
   - **Output**: `shared/models/work_item.py` with `WorkItem` and `WorkItemEvent` models. Enums `WorkItemStatus` and `WorkItemType` in `shared/contracts/dto/work_item.py`. Models registered in `shared/models/__init__.py`.
   - **Test**: Unit test validates model instantiation, enum values, default fields. Test valid status transitions matrix.
   - Fields:
     - WorkItem: id (wi-{nanoid}), project_id (FK), type, title, description, status, priority, acceptance_criteria, current_iteration, max_iterations (default 3), created_by
     - WorkItemEvent: id (auto), work_item_id (FK), event_type, from_status, to_status, iteration, details (JSON), actor

2. [ ] Add work_item_id + iteration to Task model
   - **Input**: `shared/models/task.py`
   - **Output**: `Task` model gets `work_item_id: Mapped[str | None]` (FK → work_items.id) and `iteration: Mapped[int | None]`. Both nullable for backward compat.
   - **Test**: Unit test: Task with and without work_item_id. Existing task tests still pass.

3. [ ] Alembic migration
   - **Input**: Models from steps 1-2
   - **Output**: `services/api/migrations/versions/<hash>_add_work_items.py` — creates `work_items` table, `work_item_events` table, adds `work_item_id` + `iteration` columns to `tasks`. Indices on work_items(project_id, status, priority) and work_item_events(work_item_id).
   - **Test**: `make migrate` succeeds. `make test-api-unit` still passes (no regressions). ⚠️ needs-approval (DB schema change)

4. [ ] WorkItem API schemas (Pydantic)
   - **Input**: `services/api/src/schemas/task.py` (pattern reference), DTOs from step 1
   - **Output**: `services/api/src/schemas/work_item.py` — `WorkItemCreate`, `WorkItemRead`, `WorkItemUpdate`, `WorkItemEventRead`. WorkItemRead includes `last_event` summary and `elapsed_minutes`.
   - **Test**: Unit test: schema validation, serialization round-trip.

5. [ ] WorkItem CRUD router
   - **Input**: `services/api/src/routers/tasks.py` (pattern reference), schemas from step 4
   - **Output**: `services/api/src/routers/work_items.py` registered in `__init__.py` and `main.py`. Endpoints:
     - `POST /api/work-items/` — create work item
     - `GET /api/work-items/` — list (filters: project_id, status, type)
     - `GET /api/work-items/{id}` — get single (includes events summary)
     - `PATCH /api/work-items/{id}` — update title, description, priority, acceptance_criteria
     - `DELETE /api/work-items/{id}` — soft-delete (status → cancelled)
   - **Test**: Unit tests for each endpoint (create, list with filters, get, update, delete). Follow existing pattern from `test_create_project.py`.

6. [ ] Action endpoints (state machine transitions)
   - **Input**: Router from step 5, status enums from step 1
   - **Output**: Action endpoints added to the work_items router:
     - `POST /api/work-items/{id}/start` — backlog/todo → in_dev (creates iteration_start event)
     - `POST /api/work-items/{id}/complete` — testing → done (creates event)
     - `POST /api/work-items/{id}/fail` — any active → failed (creates event with reason)
     - `POST /api/work-items/{id}/reopen` — done/failed → backlog (creates event with reason)
     - `POST /api/work-items/{id}/transition` — generic transition with validation (for system use)
     Each action validates the transition is legal and creates a WorkItemEvent.
   - **Test**: Unit tests: valid transitions succeed, invalid transitions return 422. Event history recorded correctly.

7. [ ] WorkItem events sub-router
   - **Input**: Router from step 5, WorkItemEvent model
   - **Output**: Endpoints:
     - `GET /api/work-items/{id}/events` — list events for work item (ordered by created_at)
     - `POST /api/work-items/{id}/events` — add event (for system/workers: iteration_start, iteration_end, note)
   - **Test**: Unit tests: create event, list events, filter by event_type.

8. [ ] Integration test: WorkItem full lifecycle
   - **Input**: All previous steps
   - **Output**: `services/api/tests/integration/test_work_item_lifecycle.py` — test full flow: create → start → (add events) → complete. Verify events history. Test transition validation (invalid transitions rejected). Test WorkItem ↔ Task linkage.
   - **Test**: `make test-api-integration` passes.

9. [ ] Backlog migration script
   - **Input**: `docs/backlog.md` (Queue section), API from steps 5-6
   - **Output**: `scripts/migrate_backlog.py` — parses Queue section of backlog.md, creates WorkItems via API (curl or direct DB). Only migrates Queue items (not Done/Ideas). Maps: title, brief → description, priority (order in Queue), status=todo.
   - **Test**: Run script, verify work items created with correct data via `GET /api/work-items/`.

10. [ ] Cleanup and docs
    - **Input**: All previous steps
    - **Output**: Update brainstorm status to `in_progress`. Add entry to CHANGELOG.md. Verify `make test-unit` and `make lint` pass.
    - **Test**: `make test-unit` green, `make lint` clean.
