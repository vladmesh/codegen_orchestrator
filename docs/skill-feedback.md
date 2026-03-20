# Skill Feedback

Entries are added by skills during execution when they encounter issues caused by the skill prompt itself.
Processed by `/optimize` — obvious fixes applied automatically (with diff review), non-obvious items brought to user.

## [test-maintenance] — 2026-03-19
- **Type**: infrastructure
- **Problem**: First-run review of all tests + all code is extremely context-heavy. The skill says "if first run, review all test files" but doesn't suggest parallelizing by suite or prioritizing by staleness signals from git blame.
- **Suggested fix**: Add guidance for first run: "Use parallel subagents per suite. Prioritize suites with tests older than the most recent CHANGELOG entries — these are most likely to be stale."

## [test-maintenance] — 2026-03-19
- **Type**: stale-pattern
- **Problem**: Phase 4 (now Phase 3/4) says to run service tests "for each service that changed since last_commit" but on first run (null last_commit) doesn't clarify which services to run. Had to infer "all services with service tests."
- **Suggested fix**: Add explicit guidance: "If first run, run service tests for all services that have a tests/service/ directory."

<!-- entries below -->

## [e2e-run] — 2026-03-20
- **Type**: bug
- **Problem**: QA fail → fix → redeploy cycle doesn't verify deployed SHA matches latest main HEAD. Deploy-worker can reuse a completed deploy workflow run, leaving the fix commit undeployed.
- **Suggested fix**: After deploy completes in QA fix cycle, add SHA check: compare `deployed_sha` from service-deployment record vs latest main commit. If mismatch, re-trigger deploy.

- **Type**: missing-info
- **Problem**: Claude Code `--output-format json` wraps output in `{"type":"result",...,"result":"..."}` envelope. QA worker tries to parse the whole thing as the expected QA JSON, causing "Failed to parse QA output" error.
- **Suggested fix**: This is a qa-worker code bug, not a skill issue. But the skill should mention it in "Common Gotchas" so E2E runners know this failure mode.

## [e2e-run] — 2026-03-19 (run 2)
- **Type**: missing-info
- **Quote**: "Step 0: Health check + pre-flight cleanup" — pre-flight cleanup section
- **Problem**: Pre-flight cleanup does not clean applications API records from previous runs. The applications API `project_id` filter doesn't actually work (returns all apps), which causes QA runner to pick up unrelated application records (e.g., `codegen_orchestrator` instead of `weather-bot`). The skill should note this as a known issue and suggest using `service-deployments` API for server resolution.
- **Suggested fix**: Add a note in "Common Gotchas" about the applications API filter being broken. QA runner will fail if stale application records exist. Workaround: use `service-deployments` API which correctly filters by project_id.

## [e2e-run] — 2026-03-19
- **Type**: bug
- **Quote**: "Step 5: Monitor PR Review & Deploy" — webhook failure section
- **Problem**: Two deploy messages were published to deploy:queue — first with `action=feature` (wrong), second with `action=create`. The skill doesn't mention that the webhook and PR poller can race and produce duplicate messages with different actions. Also, the first deploy attempt consumed the `feature` message and failed on the TG_BOT_IMAGE env var interpolation (because the first deploy.yml run hadn't built it yet), causing a confusing cascade.
- **Suggested fix**: Add a note to Step 5 about checking deploy:queue for duplicate messages after PR merge. If multiple messages exist, consider clearing the stale one before the deploy-worker picks it up.

## [escort] — 2026-03-13 (first real run)
- **Type**: bug
- **Quote**: "Find what the user recently submitted. Look for stories created in the last 10 minutes"
- **Problem**: Discovery correctly found the story, but the skill doesn't mention that the architect queue may be clogged with stale messages from test runs. Should add a "queue health check" step after discovery to avoid waiting hours for a blocked architect consumer.
- **Suggested fix**: Add a step between Discovery and Architect monitoring: "Check architect:queue health — if >10 pending messages, investigate and clear stale ones."

## [escort] — 2026-03-13
- **Type**: bug
- **Quote**: Step 2 "Monitor Architect Phase" — "If architect hasn't run after 2 minutes"
- **Problem**: The skill says to check scheduler logs if architect is late, but the architect is a separate container. The logs command should be `docker compose logs architect` not `docker compose logs scheduler`. Also, the skill doesn't mention checking queue depth/position — knowing that our story is at position 55 of 68 is more actionable than just "hasn't run yet".
- **Suggested fix**: (a) Fix log command to `docker compose logs architect`. (b) Add queue introspection: check `xpending` and `xlen` on `architect:queue` to understand queue position.

## [escort] — 2026-03-13
- **Type**: missing
- **Problem**: The skill doesn't mention that story must be in `in_progress` status for deploy to trigger. If tasks are created outside the normal flow (manual, escort), the story stays `created` and the dispatcher never completes it. The skill should proactively check story status when all tasks complete.
- **Suggested fix**: Add a check after all tasks reach `done`: "Verify story status is `in_progress`. If still `created`, transition via `POST /api/stories/{id}/start`."

## [escort] — 2026-03-13
- **Type**: optimization
- **Problem**: The skill's Quick Reference section has `from shared.github_app import GitHubAppClient` which is wrong — the correct import is `from shared.clients.github import GitHubAppClient`.
- **Suggested fix**: Fix the import in the Quick Reference section.

## [e2e-run] — 2026-03-16
- **Type**: bug
- **Quote**: `"Step 1b ... MESSAGE_TEXT=$MESSAGE_TEXT"`
- **Problem**: PO may create project with hyphenated name (`weather-bot`) even when user says `weather_bot`. The skill uses `PROJECT_NAME` with underscores throughout but PO uses hyphens. Step 1d lookup by name fails. Should define both `PROJECT_NAME` and `REPO_SLUG` early and search by either.
- **Suggested fix**: After PO creates the project, search API by both underscore and hyphen variants. Or always use the API-returned name.

## [e2e-run] — 2026-03-16
- **Type**: bug
- **Quote**: `"curl -s http://localhost:8000/wm-api/workers/"` (Step 4 polling loop)
- **Problem**: The WM introspection API returns `{"detail":"Not Found"}` at `/wm-api/workers/`. The endpoint may have changed or requires trailing slash handling. The polling loop crashes with `TypeError: string indices must be integers`.
- **Suggested fix**: Verify correct WM API endpoint and add error handling for non-JSON / non-list responses in the polling script.

## [e2e-run] — 2026-03-16
- **Type**: missing-info
- **Quote**: Step 5 "Deploy is triggered later by the webhook when the PR is merged"
- **Problem**: For newly scaffolded repos, the GitHub webhook may not be configured. The webhook never arrived after PR merge. The skill doesn't mention this as a known failure mode or provide a workaround (manually publish deploy message + create Run record).
- **Suggested fix**: Add a note: "If webhook doesn't arrive within 60s of merge, check if repo has org-level webhook. Workaround: create Run record via API, publish DeployMessage to deploy:queue manually."

## [e2e-run] — 2026-03-16
- **Type**: missing-info
- **Quote**: Step 5 deploy message publishing
- **Problem**: The skill doesn't document how to manually publish a deploy message. DeployMessage requires `task_id` which is actually a `run_id` (format `deploy-wh-{hex}`). Must create Run record first via `POST /api/runs/` with field `type` (not `run_type`).
- **Suggested fix**: Add a "Manual deploy trigger" recipe to the skill with exact API calls and correct field names.

## [plan] — 2026-03-08
- **Type**: bug
- **Quote**: "Run `make test-unit` at minimum" (from CLAUDE.md) vs plan step 5 "Run `make test-langgraph-unit`"
- **Problem**: `make test-langgraph-unit` does not exist. The Makefile pattern is `make test-{service}-unit` but `langgraph` is not a valid service target — only `api`, `scheduler`, `telegram` work. The langgraph tests run as part of `make test-unit`.
- **Suggested fix**: In plan template, use `make test-unit` instead of `make test-langgraph-unit`. Or document the valid service targets.

## [implement] — 2026-03-08
- **Type**: optimization
- **Quote**: "**Rebuild and restart services** (MANDATORY before any testing): `make rebuild`"
- **Problem**: For pure refactoring tasks (no runtime behavior change, only code reorganization), `make rebuild` is unnecessary overhead. The gate should be conditional on whether the task changes runtime behavior.
- **Suggested fix**: Add a note: "Skip `make rebuild` for pure refactoring tasks (file splits, renames, import changes) where no runtime behavior changes. CI passing is sufficient validation."

## [implement] — 2026-03-08 (task #54)
- **Type**: bug
- **Quote**: Step 1 — `curl -sf -X POST "http://localhost:8000/api/tasks/$WI_ID/start"`
- **Problem**: `/start` endpoint returned 200 but did NOT transition the task from `backlog` to `in_dev`. Had to call `/transition?to_status=in_dev` separately. Either the `/start` endpoint behavior changed or the skill assumes it does more than it does.
- **Suggested fix**: After `/start`, verify status. If still `backlog`, call `/transition?to_status=in_dev` explicitly. Or fix the `/start` endpoint to actually transition.

## [implement] — 2026-03-08 (task #54)
- **Type**: bug
- **Quote**: Step 5 TDD — running tests with `python -m pytest` and `make test-langgraph-unit`
- **Problem**: `python` is not in PATH (only available inside containers). `make test-langgraph-unit` doesn't exist. Must always use `make test-unit` from project root.
- **Suggested fix**: Add to skill: "Always run tests via `make test-unit` from project root. Do NOT use `python -m pytest` directly — Python is only available inside Docker containers."

## [implement] — 2026-03-08 (task #54)
- **Type**: bug
- **Quote**: Step 8 — "Switch to main and pull: `git checkout main && git pull`"
- **Problem**: Local main diverges from remote due to accumulated doc-only commits (plans, changelogs, backlog) that are never pushed. After a squash-merged PR, `git pull` fails with divergent branches. Had to `git reset --hard origin/main` which is destructive.
- **Suggested fix**: Change step 8 to: `git checkout main && git fetch origin && git reset --hard origin/main`. This is safe because all doc-only commits are regenerated from API anyway. Or: push doc-only commits before creating the PR branch to prevent divergence.

## [e2e-run] — 2026-03-16 (run 2)
- **Type**: bug
- **Quote**: "Step 0: Pre-flight cleanup — Clean stale deployments on servers"
- **Problem**: Pre-flight cleanup checks for `/opt/services/$PROJECT_NAME` (underscored) but PO creates the project with hyphens (`weather-bot`). Cleanup missed the stale deployment because it looked for `weather_bot`. This caused port conflicts and container name collisions during deploy verification.
- **Suggested fix**: Always check both `$PROJECT_NAME` and `$REPO_SLUG` variants on the server during pre-flight cleanup.

## [e2e-run] — 2026-03-16 (run 2)
- **Type**: bug
- **Quote**: "Branch protection requires check 'ci'" (Problem 1 from previous run, marked 'done')
- **Problem**: Branch protection check name mismatch (`ci` vs `lint-and-test`) recurred in this run. The previous fix only updated the existing repo's protection — it didn't fix the scaffolder/deploy-worker code that sets up branch protection for NEW repos.
- **Suggested fix**: Fix the branch protection setup code (likely in scaffolder or deploy-worker) to use `lint-and-test` as the required context name, not `ci`.


## [e2e-run] — 2026-03-16
- **Type**: bug
- **Quote**: "DELETE FROM applications WHERE repo_id IN (SELECT id FROM repositories WHERE project_id = '$PROJECT_ID'));"
- **Problem**: Extra closing parenthesis in the cleanup SQL (Step 9, item 5) causes psql syntax error. Line 8 of the DELETE cascade has `...PROJECT_ID'));` instead of `...PROJECT_ID');`
- **Suggested fix**: Remove the extra `)` on the `DELETE FROM applications` line in Step 9 cleanup SQL.

## [e2e-run] — 2026-03-16 (run 4)
- **Type**: bug
- **Quote**: Step 0 pre-flight cleanup checks both `$PROJECT_NAME` and `$REPO_SLUG` dirs on server
- **Problem**: Pre-flight cleanup correctly checks both naming variants on the server, but the old `weather-bot` deployment had running containers occupying port 8012. The `docker compose down` in cleanup relies on compose files existing in the directory. If the directory structure is intact but compose down fails silently, port stays occupied. The cleanup didn't verify port was actually freed.
- **Suggested fix**: After server cleanup, add a port verification step: `ss -tlnp | grep $PORT` and fail/warn if still occupied.

## [e2e-run] — 2026-03-16 (run 4)
- **Type**: missing-info
- **Quote**: Step 5.5 "Monitor QA Phase"
- **Problem**: When manually retriggering deploy (webhook failure workaround), QA phase was skipped — story went directly `deploying → completed`. The skill doesn't document that manual deploy retrigger may bypass QA, and doesn't provide a workaround to manually trigger QA.
- **Suggested fix**: Add note in "Webhook failure & manual deploy trigger" section: "Manual deploy retrigger may skip QA. If story goes to `completed` without `testing` phase, manually publish QAMessage to qa:queue to trigger QA."

## [e2e-run] — 2026-03-17
- **Type**: missing-info
- **Quote**: "Story API: Action-based endpoints" — `POST /api/stories/{id}/reopen → completed/failed → in_progress`
- **Problem**: `reopen` does NOT transition to `in_progress` — it transitions to `reopened`. You then need a separate `POST /start` to get to `in_progress`. The skill documents `reopen` as going directly to `in_progress`, which caused a wasted step.
- **Suggested fix**: Update the Story API reference: `reopen → reopened`, then `start → in_progress`. Or document the two-step: fail → reopen → start.

## [e2e-run] — 2026-03-17 (2)
- **Type**: missing-info
- **Quote**: Step 5 "Monitor PR Review & Deploy" — no mention of CI failure without webhook
- **Problem**: The skill documents webhook failure for *merged* PRs (manual deploy trigger), but has no recipe for CI *failure* on an open PR when webhook doesn't fire. The scheduler only polls for merged PRs. Had to manually create fix tasks and reopen stories.
- **Suggested fix**: Add a "CI failure without webhook" subsection: check CI status via `get_latest_workflow_run`, if failed → create fix task, fail/reopen/start story. This is a common case for new repos.

## [e2e-run] — 2026-03-18
- **Type**: missing-info
- **Quote**: Step 4 "Monitor Engineering" — no mention of `cancelled` → `todo` transition being blocked
- **Problem**: When the supervisor cancels tasks (due to a failed dependency), the API does not allow transitioning from `cancelled` → `backlog` → `todo` via the normal `/transition` endpoint. Had to use direct SQL UPDATE to reset cancelled tasks.
- **Suggested fix**: Document that `cancelled` status requires SQL intervention to reset, or fix the API to allow `cancelled → backlog` transitions.

## [e2e-run] — 2026-03-19
- **Type**: missing-info
- **Quote**: "Step 0: pre-flight cleanup — Clean stale deployments on servers"
- **Problem**: Pre-flight cleanup checked server directory and reported CLEAN, but deploy-worker later found `/opt/services/weather-bot/` exists and auto-fell back to `action=feature`. The pre-flight SSH check may have failed silently or the directory was created between cleanup and deploy by an earlier failed deploy run in the same test.
- **Suggested fix**: Add a note that pre-flight cleanup should also clear deploy:queue and check for in-flight deploy runs. The cleanup/deploy race window exists when pr_poller triggers deploys faster than cleanup runs.
