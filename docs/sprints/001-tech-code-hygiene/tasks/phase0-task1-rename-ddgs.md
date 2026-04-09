# Phase 0 Task 1: Rename duckduckgo_search → ddgs

## Description
Package `duckduckgo_search` has been renamed to `ddgs`. Runtime warning in logs. Update dependency name, import, test patches, and regenerate lock file.

## Files to change
- `services/langgraph/pyproject.toml:38` — `"duckduckgo-search>=7.0.0"` → `"ddgs>=7.0.0"`
- `services/langgraph/src/agents/po/tools.py:124` — `from duckduckgo_search import DDGS` → `from ddgs import DDGS`
- `services/langgraph/tests/unit/po/test_tools.py:640,656,669,682` — patch target `duckduckgo_search.DDGS` → `ddgs.DDGS`
- `services/langgraph/requirements.lock` — regenerate via `make lock-deps`

## Tests First
- Existing tests in `test_tools.py` must pass with updated patch targets
- `make test-langgraph-unit` passes

## Acceptance Criteria
- [x] No `duckduckgo_search` or `duckduckgo-search` references remain in code (docs/backlog excluded)
- [x] `make lock-deps` succeeds and lock file reflects `ddgs`
- [x] `make test-langgraph-unit` passes
- [x] `make lint` passes

## Status: done

## Developer Notes
Straightforward rename. The `ddgs` package is a drop-in replacement — same `DDGS` class, same API. Lock file regenerated cleanly; `primp` added as new transitive dep.
