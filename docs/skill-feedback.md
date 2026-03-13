# Skill Feedback

Entries are added by skills during execution when they encounter issues caused by the skill prompt itself.
Processed by `/optimize` — obvious fixes applied automatically (with diff review), non-obvious items brought to user.

<!-- entries below -->

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
