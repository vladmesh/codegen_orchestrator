# make sync — генерация docs из БД (backlog, roadmap, status, recent plans/brainstorms)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Data has moved to the DB (tasks, brainstorms, milestones), but local docs files remain useful as read-only mirrors for IDE browsing and agent context injection. Currently `make backlog` and `make roadmap` exist as generators. Need to add: `make status`, `make recent-artifacts`, `make sync` (umbrella), `POST /api/tasks/push`, `make task`, event read/write in skills, and DEV_PIPELINE.md update.

Existing patterns: `scripts/generate_backlog.py` and `scripts/generate_roadmap.py` use httpx async to hit API and render markdown. Follow the same pattern for new scripts.

The list_tasks endpoint currently lacks `source_brainstorm_id` filter — needed for sibling task lookup.

## Steps

1. [ ] `POST /api/tasks/push` endpoint
   - **Input**: `services/api/src/routers/tasks.py`, `services/api/src/schemas/task.py`
   - **Output**: New endpoint that accepts TaskCreate body, computes `priority = min(backlog priorities) - 1`, creates task. Returns TaskRead.
   - **Test**: Unit test: push creates task with priority lower than existing min. Push to empty backlog gives priority -1. Two pushes in a row give decreasing priorities.

2. [ ] Add `source_brainstorm_id` filter to list_tasks
   - **Input**: `services/api/src/routers/tasks.py` (list_tasks endpoint)
   - **Output**: New optional query param `source_brainstorm_id` on `GET /api/tasks/`. Filters tasks by matching field.
   - **Test**: Unit test: filter returns only tasks with matching brainstorm_id, empty result for non-existent id.

3. [ ] `scripts/generate_status.py` + `make status`
   - **Input**: `scripts/generate_backlog.py` (pattern), API endpoints: tasks (in_dev, recent done), events
   - **Output**: Script generates `docs/STATUS.md` with: current in_dev task + its recent events, last 3 completed tasks, task stats, quick links. Makefile target `status`.
   - **Test**: Unit test: mock API responses, verify generated markdown contains expected sections (Current Task, Recent Events, Completed, Stats).

4. [ ] `scripts/sync_recent_artifacts.py` + `make recent-artifacts`
   - **Input**: API endpoints: tasks (in_dev + last 3 done with plans/brainstorms), brainstorms
   - **Output**: Script fetches in_dev + 3 most recent done tasks. For each, writes plan to `docs/plans/<tag>-<slug>.md` and brainstorm (if source_brainstorm_id) to `docs/brainstorms/<slug>.md`. Deletes files NOT in the active window. Makefile target `recent-artifacts`.
   - **Test**: Unit test: mock API, verify correct files written and old files deleted (use tmp_path).

5. [ ] `make sync` umbrella + `make task` CLI wrapper
   - **Input**: Makefile
   - **Output**: `sync: backlog roadmap status recent-artifacts` target. `task` target: calls push endpoint via curl with `TITLE` and optional `DESC` vars.
   - **Test**: Manual smoke test (make sync runs without error, make task creates a task).

6. [ ] Event writing in /implement SKILL.md
   - **Input**: `.claude/skills/implement/SKILL.md`
   - **Output**: Add event writes: `ci_fix` note after CI fix commits, `plan_deviation` comment when deviating from plan, `implementation_summary` comment in step 9 report. All best-effort (|| true).
   - **Test**: Review — verify curl commands are syntactically correct and match TaskEventCreate schema.

7. [ ] Event reading in /implement and /plan SKILL.md
   - **Input**: `.claude/skills/implement/SKILL.md`, `.claude/skills/plan/SKILL.md`
   - **Output**: In /implement step 1 (load context): if task is in_dev (resume) or has events, fetch `GET /api/tasks/{id}/events` and print summary. Fetch sibling tasks via `source_brainstorm_id` filter, print their last_event. Same in /plan step 2 (load context).
   - **Test**: Review — verify curl commands and jq parsing are correct.

8. [ ] Integrate `make sync` into skills
   - **Input**: `.claude/skills/implement/SKILL.md`, `.claude/skills/plan/SKILL.md`, `.claude/skills/triage/SKILL.md`, `.claude/skills/checkpoint/SKILL.md`
   - **Output**: Add `make sync` call at the beginning of each skill's protocol (after loading task, before research). Checkpoint already does `make backlog` — replace with `make sync`.
   - **Test**: Review — verify make sync is called early in each skill.

9. [ ] Cleanup old artifacts + update DEV_PIPELINE.md
   - **Input**: `docs/plans/`, `docs/brainstorms/`, `docs/e2e_results/`, `docs/DEV_PIPELINE.md`, `docs/STATUS.md`
   - **Output**: Delete old plan/brainstorm/e2e files (data is in DB). Update DEV_PIPELINE.md to describe: make sync flow, push endpoint, artifact retention policy, event logging in /implement. Remove DEPRECATED warnings from STATUS.md (it's now generated).
   - **Test**: Verify docs/plans/ and docs/brainstorms/ are clean after `make sync`. DEV_PIPELINE.md accurately reflects new workflow.

