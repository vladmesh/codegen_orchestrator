# Plan: /implement work item events (#57)

## Context

Brainstorm: [orchestrator-v2-task-management.md](../brainstorms/orchestrator-v2-task-management.md) — Step 2.

Steps 0-1 delivered the WorkItem model, API, and `/next` skill on API. Now `/implement` needs to emit events so we get step-level progress tracking in the DB.

**Current state:**
- API has `POST /api/work-items/{id}/events` — accepts `event_type`, `iteration`, `details`, `actor`
- `WorkItemEventType` enum has: `status_change`, `iteration_start`, `iteration_end`, `note`
- `/implement` skill (`SKILL.md`) has no API calls — reads/writes only markdown files
- `/next` writes `#<tag> <Title>` to STATUS.md but NOT the `work_item_id` (e.g. `wi-3372a29b`)
- `/implement` needs `work_item_id` to call the events API

**What needs to change:**
1. Add `step_start` and `step_done` event types to the enum
2. `/next` must persist `work_item_id` in STATUS.md so `/implement` can use it
3. `/implement` must emit events at step boundaries and on task completion

## Steps

1. [ ] Add `step_start` / `step_done` event types
   - **Input**: `shared/contracts/dto/work_item.py`
   - **Output**: `STEP_START = "step_start"`, `STEP_DONE = "step_done"` added to `WorkItemEventType`
   - **Test**: unit test in `shared/tests/unit/test_work_item_model.py` — assert new types exist and are valid `WorkItemEventType` members
   - ⚠️ changes `shared/contracts/`

2. [ ] Add `work_item_id` field to STATUS.md via `/next` skill
   - **Input**: `.claude/skills/next/SKILL.md`, `docs/STATUS.md`
   - **Output**: `/next` skill writes `- **WorkItem**: <work_item_id>` line in STATUS.md Current Task section. Update current STATUS.md to include `wi-3372a29b` for active task.
   - **Test**: manual — run `/next` on a test item and verify STATUS.md has the id. No automated test needed (skill is a prompt, not code).

3. [ ] Update `/implement` skill to emit events via API
   - **Input**: `.claude/skills/implement/SKILL.md`
   - **Output**: Skill instructions updated:
     - On load context (step 1): read `WorkItem` field from STATUS.md, store as `$WI_ID`
     - Before TDD cycle (step 3): `curl -s -X POST .../events` with `step_start`, details: `{step: N, title: "..."}`
     - After step commit (step 4): `curl -s -X POST .../events` with `step_done`, details: `{step: N, title: "...", commit_sha: "..."}`
     - On task completion (step 6): `curl -s -X POST .../$WI_ID/complete` with actor `claude`
     - All curl calls are best-effort (don't block on failure — `|| true`)
   - **Test**: manual — no automated test for skill prompts

4. [ ] Integration test — step events through API
   - **Input**: `services/api/tests/service/test_work_item_lifecycle.py` (or new file)
   - **Output**: test that creates a work item, starts it, posts `step_start` + `step_done` events, completes it, then verifies events list via GET
   - **Test**: `make test-api-integration` (runs in CI)
