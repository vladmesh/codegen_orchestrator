# Skill Feedback

Entries are added by skills during execution when they encounter issues caused by the skill prompt itself.
Processed by `/optimize` — obvious fixes applied automatically (with diff review), non-obvious items brought to user.

<!-- entries below -->

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
