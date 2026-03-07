# Plan: Skills → API + Simplified Model (#58)

## Context

Steps 0-2 of the orchestrator-v2-task-management brainstorm are complete.
This task was originally "Step 3: /triage + /checkpoint via API", but scope
expanded after architectural review:

- **Plans as text field** — no more `docs/plans/*.md`, plan stored in `work_item.plan`
- **No structured steps** — tasks are atomic, progress tracked by git branch + commits
- **STATUS.md removed** as source of truth — API is the only source
- **`/next` eliminated** — absorbed into `/implement` (just `POST /start`)
- **step_start/step_done events removed** — unnecessary without structured steps

### What changes
- WorkItem model: add `plan` text field, add `project_id` to WorkItemUpdate
- Remove step_start/step_done event types
- API: add `since` filter, `/stats`, `/next-tag`
- `/plan` → writes to work_item.plan via API
- `/implement` → reads plan from API, creates git branch, absorbs `/next`
- `/triage` → creates work items via API
- `/checkpoint` → reads stats from API
- `backlog.md` → generated from DB (`make backlog`)
- `docs/plans/*.md`, `STATUS.md` → removed

## Steps

1. [ ] Model + API changes
   - **Input**: `shared/models/work_item.py`, `shared/contracts/dto/work_item.py`, `services/api/src/routers/work_items.py`, `services/api/src/schemas/work_item.py`
   - **Output**:
     - Migration: add `plan` text column to `work_items`
     - Remove `STEP_START`/`STEP_DONE` from `WorkItemEventType`
     - `WorkItemUpdate` schema: add `project_id` and `plan` fields
     - `WorkItemRead` schema: include `plan` field
     - `GET /api/work-items/?since=<ISO datetime>` — filter by `updated_at >= since`
     - `GET /api/work-items/stats` — `{backlog: N, todo: N, in_dev: N, done: N, ...}`
     - `GET /api/work-items/next-tag` — `{"next_tag": 61}` (max tag + 1)
   - **Test**: Unit tests for new endpoints, updated schema tests, removed step event references

2. [ ] Backlog generation script
   - **Input**: Work Items API, `docs/backlog.md` (current format as reference)
   - **Output**:
     - `scripts/generate_backlog.py` — fetches work items from API, generates `docs/backlog.md`
     - Sections: Queue (status=backlog, by priority), Done (last 10, status=done)
     - Ideas section: read from `docs/ideas.md` (standalone file, manually maintained)
     - `Makefile` target: `make backlog`
   - **Test**: Unit test with mocked API responses

3. [ ] Update `/plan` skill
   - **Input**: `.claude/skills/plan/SKILL.md`
   - **Output**: Updated skill that:
     - Writes plan text to work item via `PATCH /api/work-items/{id}` (`plan` field)
     - No longer creates `docs/plans/*.md`
     - No longer updates STATUS.md
   - **Test**: Manual — run `/plan`, verify plan text in API response

4. [ ] Update `/implement` skill
   - **Input**: `.claude/skills/implement/SKILL.md`
   - **Output**: Updated skill that:
     - On start: queries `GET /api/work-items/?status=in_dev&limit=1` or accepts `#ID` argument
     - If work item not started: calls `POST /start` (absorbs `/next`)
     - Reads plan from `work_item.plan` field (API response)
     - Creates git branch `wi/{tag}-{slug}` and works there
     - No more step_start/step_done event calls
     - No STATUS.md reads/writes
     - On completion: `POST /complete`, update CHANGELOG + `make backlog`, merge branch
     - Removes all step tracking logic
   - **Test**: Manual — run `/implement`, verify branch created, plan read from API

5. [ ] Update `/triage` skill
   - **Input**: `.claude/skills/triage/SKILL.md`
   - **Output**: Updated skill that:
     - Creates tasks via `POST /api/work-items/` with `project_id: "codegen-orchestrator"`
     - Gets next tag via `GET /api/work-items/next-tag`
     - Dedup/regression via API queries
     - Runs `make backlog` after changes
     - No direct backlog.md editing
   - **Test**: Manual — run triage on test brainstorm

6. [ ] Update `/checkpoint` skill
   - **Input**: `.claude/skills/checkpoint/SKILL.md`
   - **Output**: Updated skill that:
     - Stats via `GET /api/work-items/stats`
     - Recently completed via `GET /api/work-items/?status=done&since=<date>`
     - Runs `make backlog` after triage step
   - **Test**: Manual — run checkpoint

7. [ ] Cleanup
   - **Input**: `docs/STATUS.md`, `docs/plans/`, `.claude/skills/next/`, `docs/backlog.md`
   - **Output**:
     - Delete `.claude/skills/next/` (absorbed into `/implement`)
     - Delete `docs/STATUS.md` (API is source of truth)
     - Move Ideas from `docs/backlog.md` to `docs/ideas.md`
     - Delete existing `docs/plans/*.md` (migrate any active plan to work item)
     - Remove step_start/step_done references from service tests
     - Update CLAUDE.md if it references STATUS.md or /next
   - **Test**: `make test-unit` passes, no broken references

8. [ ] Integration tests + service tests
   - **Input**: `services/api/tests/`
   - **Output**: Service tests for `since`, `/stats`, `/next-tag`, `plan` field PATCH
   - **Test**: CI green
