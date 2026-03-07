# Plan: /triage + /checkpoint via API (#58)

## Context

Steps 0-2 of the orchestrator-v2-task-management brainstorm are complete:
- Step 0: WorkItem model + API + backlog migration
- Step 1: `/next` skill uses API
- Step 2: `/implement` emits work item events

This is Step 3: migrate `/triage` and `/checkpoint` skills to use the Work Items API
instead of directly editing `backlog.md`. After this, `backlog.md` becomes a read-only
view generated from the database.

### Current state
- `/triage` parses markdown reports, writes new tasks directly into `backlog.md`
- `/checkpoint` reads markdown files to count progress, manually updates docs
- Both skills assign tag IDs by scanning backlog.md for max existing ID
- Work items API exists with CRUD, action endpoints, and events
- API list endpoint supports `status`, `type`, `project_id`, `limit`, `sort` filters
- API does NOT have: `since` date filter, next-tag endpoint, stats/counts

### What changes
- `/triage`: creates work items via `POST /api/work-items` instead of editing backlog.md
- `/checkpoint`: queries `GET /api/work-items?status=done` for progress stats
- New API endpoints: `since` filter, next-tag, stats
- `backlog.md` regenerated from DB after each triage (read-only view)

## Steps

1. [ ] API: add `since` filter, `/stats`, `/next-tag`, and `project_id` in WorkItemUpdate
   - **Input**: `services/api/src/routers/work_items.py`, `services/api/src/schemas/work_item.py`
   - **Output**:
     - `GET /api/work-items/?since=2026-03-01T00:00:00Z` — filters by `updated_at >= since`
     - `GET /api/work-items/stats` — returns `{backlog: N, todo: N, in_dev: N, done: N, ...}` counts by status
     - `GET /api/work-items/next-tag` — returns `{"next_tag": 61}` (max tag number + 1)
     - `WorkItemUpdate` schema: add optional `project_id: str | None` field (allows PATCH to reassign project)
   - **Test**: Unit tests for each new endpoint and for project_id update

2. [ ] Backlog generation script
   - **Input**: Work Items API, `docs/backlog.md` (current format as reference)
   - **Output**: `scripts/generate_backlog.py` — fetches work items from API, generates `docs/backlog.md` in current format. Sections: Queue (backlog status, ordered by priority), Ideas (kept as-is from a static section in the script or a separate file), Done (last 10 done items). `Makefile` target: `make backlog`
   - **Test**: Unit test with mocked API responses, verify generated markdown matches expected format

3. [ ] Update `/triage` skill to use API
   - **Input**: `.claude/skills/triage/SKILL.md`
   - **Output**: Updated skill that:
     - Creates tasks via `curl -s -X POST http://localhost:8000/api/work-items/ -H 'Content-Type: application/json' -d '{...}'` with `project_id: "codegen-orchestrator"`
     - Gets next tag via `GET /api/work-items/next-tag`
     - Dedup check via `GET /api/work-items/?status=backlog` + search by keywords (still in skill logic, not API)
     - Regression detection via `GET /api/work-items/?status=done` + keyword search
     - Reopen via `POST /api/work-items/{id}/reopen`
     - After all changes: runs `make backlog` to regenerate markdown
     - Removes direct backlog.md editing (except Ideas section — kept manually for now)
   - **Test**: Manual — run triage on a test brainstorm, verify work item created in API and backlog.md regenerated

4. [ ] Update `/checkpoint` skill to use API
   - **Input**: `.claude/skills/checkpoint/SKILL.md`
   - **Output**: Updated skill that:
     - Counts completed tasks via `GET /api/work-items/stats`
     - Gets recently completed via `GET /api/work-items/?status=done&since=<last_checkpoint_date>&sort=-created_at`
     - After triage step: runs `make backlog` to regenerate markdown
     - Rest of checkpoint logic unchanged (audit, CHANGELOG, ROADMAP, cleanup)
   - **Test**: Manual — run checkpoint, verify stats match API data

5. [ ] Move Ideas section to `docs/ideas.md` and include in generation
   - **Input**: `docs/backlog.md` Ideas section, `scripts/generate_backlog.py`
   - **Output**:
     - `docs/ideas.md` — standalone file for ideas (not in DB, manually maintained)
     - `scripts/generate_backlog.py` appends ideas.md content to generated backlog.md
     - `/triage` skill updated to add ideas to `docs/ideas.md` instead of backlog.md
   - **Test**: Verify `make backlog` produces backlog.md with Ideas section from ideas.md

6. [ ] Integration test for new API endpoints
   - **Input**: `services/api/tests/integration/`
   - **Output**: Tests for `since` filter, `/stats`, `/next-tag` endpoints against real DB
   - **Test**: `make test-api-integration`
