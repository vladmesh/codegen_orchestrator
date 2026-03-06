# Plan: US3 — Add Feature to Existing Project (#34)

## Context

Core product flow: user asks to add a feature to an already-deployed project ("допили мне бота").
The infrastructure is ~80% ready — `EngineeringMessage` already supports `action="feature"`,
the developer node has a feature task builder, worker-manager skips scaffold for non-create actions,
and the deploy pipeline reuses existing allocations.

**What's actually missing** is the end-to-end validation: nobody has ever triggered `action="feature"`
through the full pipeline. The PO prompt already describes the feature/fix scenario (prompts.py:64-69),
tools exist (`list_projects`, `trigger_engineering` with action param), but the flow has never been
tested. There are likely edge cases in project status transitions and the engineering worker that
will surface during E2E testing.

**Approach**: validate the existing code with an E2E test first, then fix whatever breaks.
No new tools or contracts needed — the plumbing exists.

## Steps

1. [ ] Dry-run feature flow via direct API/queue (no PO)
   - **Input**: An already-deployed project from a previous `action=create` E2E run (or create one fresh with `todo_api` test). Then trigger `action="feature"` via direct queue publish.
   - **Output**: Engineering worker processes the feature request: no scaffold, developer gets feature task, CI passes, auto-deploy triggers, project remains active.
   - **Test**: Manual E2E — publish `EngineeringMessage(action="feature", description="Add GET /todos/stats endpoint that returns {total, completed, pending}")` for the existing project. Verify: (1) no scaffold phase in worker-manager logs, (2) developer commits feature code, (3) CI passes, (4) deploy succeeds, (5) new endpoint responds.

2. [ ] Fix engineering worker edge cases for feature flow
   - **Input**: Findings from step 1 — likely issues with project status transitions (`draft` vs `active` vs `scaffolded`), resource allocation for already-allocated projects, or workspace setup for existing repos.
   - **Output**: Engineering worker correctly handles `action=feature` for projects in `active`/`scaffolded` status. All status transition paths work.
   - **Test**: Unit tests for `process_engineering_job()` with `action="feature"` on projects in various statuses (`active`, `scaffolded`, `draft`). Verify scaffold is skipped, repo is cloned (not created), allocations are reused.

3. [ ] Fix developer node edge cases for feature flow
   - **Input**: Findings from step 1 — the `_build_feature_task()` method and `_build_scaffold_config()` skip logic.
   - **Output**: Developer node correctly generates feature/fix task content, skips scaffold, handles existing repo setup.
   - **Test**: Unit tests for `DeveloperNode.run()` with `action="feature"` state — verify scaffold_config is None, task message uses feature template, repo is determined from `repository_url`.

4. [ ] E2E feature flow via PO agent (--with-po)
   - **Input**: A deployed project (from step 1 or fresh). Send natural-language feature request to PO: "Добавь в мой todo_api эндпоинт GET /todos/stats".
   - **Output**: PO calls `list_projects` → identifies the project → calls `trigger_engineering(project_id, action="feature", description="...")` → engineering completes → deploy succeeds.
   - **Test**: E2E with `--with-po` flag. Verify: (1) PO response mentions the project, (2) engineering task created with `action=feature`, (3) full pipeline completes.

5. [ ] Write E2E feature-add scenario into e2e-run skill
   - **Input**: Learnings from steps 1-4. The E2E skill currently only supports `action=create`.
   - **Output**: Updated `.claude/skills/e2e-run/SKILL.md` with a "Feature Add" test mode (e.g., `--feature` flag or a dedicated test matrix entry). The scenario: (1) run a create test, (2) after deploy succeeds, trigger a feature add on the same project, (3) verify the feature was added.
   - **Test**: Run the new E2E feature scenario end-to-end. Document results in `docs/e2e_results/`.

6. [ ] Update USER_STORIES.md acceptance criteria
   - **Input**: Results from steps 1-5.
   - **Output**: Check off remaining US3 acceptance criteria in `docs/USER_STORIES.md`. Update `docs/STATUS.md` and `docs/backlog.md` to mark #34 as done.
   - **Test**: All US3 acceptance criteria checked. CHANGELOG updated.
