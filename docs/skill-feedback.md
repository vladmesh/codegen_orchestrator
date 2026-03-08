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
