# #64 Implement skill: PR flow + in_ci status + need_e2e

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Task #64 addresses the broken completion flow in the /implement skill. Currently:
- `/implement` calls `/complete` from `in_dev`, which is an **invalid transition** (in_dev cannot go directly to done)
- No PR is created — CI doesn't trigger on `wi/*` branches (but DOES trigger on PRs to main)
- Statuses `in_review` and `testing` are unused
- No connection between /implement and /e2e-run

The task description specifies renaming `in_review` -> `in_ci`, adding a `need_e2e` boolean field, implementing PR-based flow in the skill, and making `/complete` smarter.

CI workflow (`ci.yml`) already triggers on `pull_request` to main — no changes needed there.

Current state:
- `TaskStatus` enum: 8 values including `IN_REVIEW` and `TESTING` (unused)
- `VALID_TRANSITIONS`: in_dev -> {in_review, testing, failed, cancelled}; in_review -> {in_dev, testing, failed, cancelled}; testing -> {done, in_dev, failed, cancelled}
- Task model: no `need_e2e` field
- `/complete` endpoint: strict validation, only allows transitions defined in VALID_TRANSITIONS
- `/transition` endpoint: generic, takes `to_status` query param
- Implement skill: pushes branch, polls CI, merges locally (no PR), calls `/complete` directly

## Steps

1. [ ] Rename IN_REVIEW -> IN_CI in status enum and transitions
   - **Input**: `shared/contracts/dto/task.py`
   - **Output**: `IN_REVIEW` renamed to `IN_CI` ("in_ci" value), transitions updated:
     - in_dev -> {in_ci, failed, cancelled}
     - in_ci -> {in_dev, testing, failed, cancelled} (NO direct done — must go through testing)
     - testing -> {done, in_dev, failed, cancelled} (unchanged)
   - **Test**: Unit tests for VALID_TRANSITIONS — verify in_ci replaces in_review, verify new allowed transitions, verify in_ci cannot go directly to done

2. [ ] Add need_e2e field to Task model and API schemas
   - **Input**: `shared/models/task.py`, `services/api/src/schemas/task.py`
   - **Output**: `need_e2e: bool = False` on Task model, added to TaskCreate, TaskRead, TaskUpdate schemas
   - **Test**: Unit tests for schema serialization with need_e2e field

3. [ ] Alembic migration for in_ci rename + need_e2e column
   - **Input**: Steps 1-2 outputs
   - **Output**: Single migration that: (a) updates existing `in_review` status values to `in_ci` in tasks table, (b) adds `need_e2e` boolean column with default false
   - **Test**: `make migrate` succeeds, verify via integration test or manual check

4. [ ] Make /complete endpoint auto-promote through intermediate statuses
   - **Input**: `services/api/src/routers/tasks.py` (complete_task function, lines 346-364)
   - **Output**: `/complete` auto-walks the state machine: in_dev -> in_ci -> testing -> done (always goes through testing, records each intermediate event). From in_ci goes to testing -> done. From testing goes directly to done. Rejects complete from backlog/cancelled.
   - **Test**: Unit tests: complete from in_dev (auto-promotes through in_ci + testing), complete from in_ci (auto-promotes through testing), complete from testing (direct to done), reject complete from backlog/cancelled

5. [ ] Rewrite implement skill — PR flow with smoke/E2E testing
   - **Input**: `.claude/skills/implement/SKILL.md`
   - **Output**: Updated step 6 (Push and CI) and step 7 (Testing + Merge) with new flow:
     **Step 6 — Push + PR + CI:**
     - `git push -u origin <branch>`
     - `gh pr create --title "#ID — title" --body "..."` targeting main
     - Transition to in_ci via API
     - Poll CI on PR: `gh run list --branch <branch> --limit 1 --json status` every 60s (max 15 min)
     - CI red: read logs (`gh run view --log-failed`), fix, commit, push, re-poll
     - CI green: proceed to step 7
     **Step 7 — Testing (smoke or E2E):**
     - Transition to testing via API
     - **Simple tasks (need_e2e=false):** Smoke test — `make build` then manually verify: curl API endpoints, check Redis streams, review structlog output for affected services, confirm no errors in `docker compose logs`
     - **Complex tasks (need_e2e=true):** Full E2E — `make build` then run Agent tool with `/e2e-run <test> --no-nuke`
     - Test red: fix, commit, push, re-poll CI, re-test
     - Test green: proceed to merge
     **Step 8 — Merge + Complete:**
     - `gh pr merge --squash` (PR must be merged by Claude, not left open)
     - `git checkout main && git pull`
     - `/complete` via API
     - Update CHANGELOG, `make backlog`, commit docs on main
   - **Test**: N/A (skill file — tested via next task run)

6. [ ] Update E2E skill API URLs post-rename
   - **Input**: `.claude/skills/e2e-run/SKILL.md`
   - **Output**: Check all API URLs in E2E skill. The rename (WorkItem->Task, Task->Run) already happened. Verify URLs use `/api/tasks/` for planning-layer and `/api/runs/` for execution-layer. Fix any stale references.
   - **Test**: N/A (skill file — verified by reading)

7. [ ] Integration test — full transition flow
   - **Input**: Steps 1-4 outputs
   - **Output**: Integration test that exercises: backlog -> todo -> in_dev -> in_ci -> testing -> done (full path). Verify events recorded for each transition. Verify /complete auto-promotes correctly from each intermediate status. Verify need_e2e field persists through create/read/update.
   - **Test**: `make test-api-integration`

