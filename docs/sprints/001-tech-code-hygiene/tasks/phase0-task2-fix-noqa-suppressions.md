# Phase 0 Task 2: Fix noqa suppressions that mask real complexity

## Description
Four noqa comments in production code suppress real issues instead of fixing them. Each has a concrete fix path.

### 1. PLR0913 — too many args in `handle_engineering_success`
**File**: `services/langgraph/src/consumers/engineering_result_handler.py:189`
**Fix**: Extract a `EngineeringSuccessParams` dataclass grouping the 11 parameters. Function takes a single params object.

### 2. PLR0911 — too many returns in `_compute_secret`
**File**: `services/langgraph/src/subgraphs/devops/secret_resolver.py:135`
**Fix**: Extract a lookup dict/table mapping key patterns to resolver functions. `_compute_secret` does a table lookup instead of a chain of if/elif.

### 3. PLR2004 — magic number in debug endpoint
**File**: `services/api/src/routers/debug.py:65`
**Fix**: Extract `HIGH_PENDING_THRESHOLD = 100` as a module-level constant.

### 4. S110 — bare `except: pass`
**File**: `services/api/src/routers/debug.py:71`
**Fix**: Catch specific exception (e.g. `redis.ResponseError`), log with structlog instead of silent pass.

## Tests First
- Existing tests for `handle_engineering_success` callers must be updated to use new dataclass
- `_compute_secret` tests must still pass after refactor to lookup table
- `make test-langgraph-unit` and `make test-api-unit` pass
- `make lint` passes (no noqa needed on these 4 lines)

## Acceptance Criteria
- [ ] All 4 `# noqa` comments removed from the listed files
- [ ] `EngineeringSuccessParams` dataclass exists, `handle_engineering_success` takes it
- [ ] `_compute_secret` uses lookup table, no if/elif chain
- [ ] Magic number 100 replaced with named constant
- [ ] Bare except replaced with specific exception + structlog warning
- [ ] `make lint` passes
- [ ] `make test-langgraph-unit` passes
- [ ] `make test-api-unit` passes

## Status: pending

## Developer Notes
_To be filled during implementation._
