# Rename WorkItem→Task, Task→Run

## Context

Renaming entities to match the target architecture: WorkItem (planning layer) becomes Task, Task (execution layer) becomes Run. Source: brainstorm `project-repo-entity-model.md` (bs-77618a86).

Current state:
- `work_items` table → planning (backlog/todo/in_dev/done)
- `tasks` table → execution (queued/running/completed/failed)
- `work_item_events` table → status history
- FK: `tasks.work_item_id` → `work_items.id`

Target:
- `tasks` table (from work_items), `runs` table (from tasks), `task_events` (from work_item_events)
- FK: `runs.task_id` → `tasks.id`
- ID prefixes: `wi-` → `task-`, execution IDs stay as-is
- API: `/api/work-items/` → `/api/tasks/`, `/api/tasks/` → `/api/runs/`

Challenge: name collision — both layers swap through "task". Order: rename execution layer first (Task→Run), then planning layer (WorkItem→Task).

~30 source files + ~15 test files affected.

## Steps

1. [ ] Alembic migration: rename tables + FK columns
   - **Input**: current schema (work_items, tasks, work_item_events tables)
   - **Output**: single migration: `tasks` → `runs`, `work_items` → `tasks`, `work_item_events` → `task_events`; rename column `runs.work_item_id` → `runs.task_id`; update all FK constraint names
   - **Test**: `make migrate` succeeds, rollback works (`make migrate` with downgrade)

2. [ ] Rename execution layer: Task→Run (models, DTOs, schemas)
   - **Input**: `shared/models/task.py`, `shared/contracts/dto/task.py`, `services/api/src/schemas/task.py`
   - **Output**: new files `run.py` in each location; classes renamed: `Task`→`Run`, `TaskStatus`→`RunStatus`, `TaskType`→`RunType`, `TaskCreate`→`RunCreate`, `TaskDTO`→`RunDTO`, `TaskRead`→`RunRead`, `TaskUpdate`→`RunUpdate`; table name `__tablename__ = "runs"`; FK column `task_id` (was `work_item_id`)
   - **Test**: unit tests pass for model instantiation, schema validation

3. [ ] Rename execution layer: router + tests
   - **Input**: `services/api/src/routers/tasks.py`, test files for tasks router
   - **Output**: `services/api/src/routers/runs.py` with prefix `/runs`; rename test files; update `routers/__init__.py`, `main.py`
   - **Test**: unit tests for runs router pass

4. [ ] Rename planning layer: WorkItem→Task (models, DTOs, schemas)
   - **Input**: `shared/models/work_item.py`, `shared/contracts/dto/work_item.py`, `services/api/src/schemas/work_item.py`
   - **Output**: new files `task.py` in each location; classes renamed: `WorkItem`→`Task`, `WorkItemEvent`→`TaskEvent`, `WorkItemStatus`→`TaskStatus`, `WorkItemType`→`TaskType`, `WorkItemEventType`→`TaskEventType`; table name `__tablename__ = "tasks"`; ID prefix `task-`
   - **Test**: unit tests pass for model, DTO transitions, schema validation

5. [ ] Rename planning layer: router + tests
   - **Input**: `services/api/src/routers/work_items.py`, test files for work_items router
   - **Output**: `services/api/src/routers/tasks.py` with prefix `/tasks`; rename test files; update all endpoint URLs in tests
   - **Test**: unit tests for tasks router pass

6. [ ] Update cross-references: workers, webhooks, related routers
   - **Input**: `services/langgraph/src/workers/engineering_worker.py`, `deploy_worker.py`, `_base.py`, `_events.py`; `services/api/src/routers/webhooks.py`, `projects.py`, `milestones.py`
   - **Output**: all imports updated (Task→Run, WorkItem→Task); milestone router endpoint `/milestones/{id}/work-items` → `/milestones/{id}/tasks`; worker references to TaskStatus/TaskType → RunStatus/RunType
   - **Test**: `make test-unit` passes

7. [ ] Update scripts + Makefile
   - **Input**: `scripts/generate_backlog.py`, `generate_roadmap.py`, `seed_milestones.py`, `enrich_work_items.py`, `migrate_backlog.py`; `scripts/tests/`; `Makefile`
   - **Output**: all API URL references updated (`/api/work-items/` → `/api/tasks/`); rename `enrich_work_items.py` → `enrich_tasks.py`; update script tests
   - **Test**: script tests pass

8. [ ] Update skills
   - **Input**: `.claude/skills/plan/SKILL.md`, `implement/SKILL.md`, `triage/SKILL.md`, `checkpoint/SKILL.md`
   - **Output**: all `/api/work-items/` URLs → `/api/tasks/`; terminology updated (work item → task)
   - **Test**: manual — read skills, verify URLs correct

9. [ ] Update __init__ exports + shared imports
   - **Input**: `shared/models/__init__.py`, `shared/contracts/dto/__init__.py`, `services/api/src/schemas/__init__.py`
   - **Output**: all exports updated with new names; old files deleted (no backward compat shims)
   - **Test**: `make test-unit` passes, `make lint` passes

10. [ ] Integration test + cleanup
    - **Input**: all changes from steps 1-9
    - **Output**: `make test-all` passes; `make backlog` generates correctly; `make roadmap` generates correctly; delete old files; update `docs/CONTRACTS.md` if it references old names
    - **Test**: full test suite green, `make lint` clean

