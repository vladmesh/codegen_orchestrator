---
name: test-maintenance
description: >
  Run integration and service tests locally, fix stale/broken tests, propose new tests for uncovered logic.
  Use when user says "run integration tests", "test maintenance", "update tests", "check test health",
  "прогони интеграционные", "актуализируй тесты", or wants to ensure test suites reflect current codebase.
  Also use proactively from /checkpoint when integration tests haven't been run in >1 week.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent
---

# Test Maintenance

Run integration and service tests, fix what's broken, propose tests for uncovered logic.

## Key References
- [docs/TESTING.md](docs/TESTING.md) — test layers, compose files, make targets, CI labels

## Context

This project has three relevant test layers (unit tests and E2E are out of scope — they serve different purposes and cannot substitute for integration/service tests):

**Integration tests** (`tests/integration/`): Multi-service Docker Compose stacks. Expensive, not run in CI regularly. Tend to go stale. There are 5 suites:
- `backend` — langgraph + engineering-worker + worker-manager + API + DinD (heaviest, ~5 min)
- `template` — copier project generation (lightest, ~1 min)
- `frontend` — telegram_bot + api (~1 min)
- `infra` — scheduler + infra-service + api (~2 min)
- `po-tools` — PO agent tools + api (~2 min)

**Service tests** (`services/{name}/tests/service/`): Single service + dependencies (DB, Redis). Run in CI, so they're always correct but may not be current. Services with test suites: api, langgraph, scheduler, telegram_bot, worker-manager, infra-service.

## Running Tests

All tests run via Make targets. No live stack needed — compose files are self-contained.

```bash
# Integration tests (one suite at a time)
make test-integration-template
make test-integration-frontend
make test-integration-infra
make test-integration-po-tools
make test-integration-backend    # heaviest, run last

# Service tests
make test-service SERVICE=api
make test-service SERVICE=langgraph
# etc.
```

Each command builds Docker images, starts the compose stack, runs pytest, and cleans up automatically. Output includes full pytest output with pass/fail per test.

## State

Last-run metadata lives in `.claude/skills/test-maintenance/state.json`:

```json
{
  "last_run": "2026-03-18",
  "last_commit": "fc302226",
  "suites_run": {
    "template": "2026-03-18",
    "frontend": "2026-03-18",
    "backend": "2026-03-15"
  }
}
```

If `state.json` has no `last_commit` — this is the first run. Use the last 2 weeks of git history as baseline for the "new logic" review.

## Protocol

### Phase 1: Review & Actualize All Tests

Before running anything, read the existing tests and compare them against the current codebase. The goal is to fix stale tests upfront so you get a clean run on the first try.

Review **both** integration and service tests in a single pass — this gives you a complete picture of what's tested where, making it easier to decide where new tests should go in Phase 2.

**Integration tests** — for each suite in `tests/integration/{suite}/`:
**Service tests** — for each service dir in `services/{name}/tests/service/`:

1. **Read the test files** and the code they're testing
2. **Identify stale assertions** — renamed fields, removed endpoints, changed contracts, new required parameters
3. **Fix or delete** stale tests:
   - Update the test to match current behavior
   - Delete if the tested functionality no longer exists (note what was being tested — it might need a replacement)
4. **Fix small code bugs** found during review — if the test is correct but the code has a minor bug (< 10 lines, obviously correct), fix the code directly

For each fix, categorize it:

**A. Stale test** — the test asserts something that was correct before but no longer matches the codebase
**B. Small code bug** — the test is correct but the code has a minor issue
**C. Significant issue** — something deeper is broken (architectural mismatch, missing migration, integration contract violation). Do NOT attempt to fix — document it in the report.

If `last_commit` is null (first run), review all test files. Otherwise, focus on tests that touch code changed since `last_commit`.

### Phase 2: Review New Logic & Write Tests

Look at what changed since `last_commit` (or last 2 weeks if first run) and identify business logic that should have integration or service test coverage.

```bash
git log --oneline <last_commit>..HEAD
git diff --stat <last_commit>..HEAD
```

Also read CHANGELOG entries since `last_run` date — they describe changes in terms of features, which is more useful than raw file paths.

For each significant change, ask:
1. Does this involve cross-service communication? → integration test
2. Does this involve a single service with DB/Redis? → service test
3. Is there already a test covering this? → check existing test files
4. Is this just a refactor with no behavior change? → no new test needed

For new tests that should be written:
- **Service tests**: add test files to `services/{name}/tests/service/`. These will automatically be picked up by the existing Dockerfile.test and compose file.
- **Integration tests**: add test files to `tests/integration/{suite}/`. Match the existing conftest fixtures.
- If a new integration suite is needed (rare), create a new compose file in `docker/test/integration/` following the existing patterns. The Makefile discovers suites automatically from `*.yml` files.

When writing new tests, follow existing patterns in the suite. Read the conftest.py to understand available fixtures.

### Phase 3: Run All Integration Tests

Now run all 5 integration suites. Start with the lighter ones so you surface remaining failures early:

1. `template` (~1 min)
2. `frontend` (~1 min)
3. `infra` (~2 min)
4. `po-tools` (~2 min)
5. `backend` (~5 min)

Run them one at a time (they share Docker resources). For each suite, capture:
- Pass/fail status per test
- Full error output for failures
- Whether the failure is a test issue or a code issue

### Phase 4: Run Changed Service Tests

Check which services changed since `last_commit`:

```bash
git diff --name-only <last_commit>..HEAD -- services/
```

For each service that changed AND has a `tests/service/` directory, run:

```bash
make test-service SERVICE=<name>
```

Skip services that haven't changed — their tests run in CI anyway. If first run (`last_commit` is null), run all services that have service tests.

### Phase 5: Triage Remaining Failures

If any tests still fail after Phase 1 actualization:

- **Stale test missed earlier** — fix it now
- **Small code bug** — fix if < 10 lines and obviously correct, re-run to confirm
- **Significant issue** — document in the report, do NOT attempt to fix

After each fix, re-run the specific suite to confirm it passes:

```bash
# For integration tests, re-run the whole suite (no way to run individual tests in Docker)
make test-integration-<suite>

# For service tests
make test-service SERVICE=<name>
```

### Phase 6: Final Green Run

After all fixes, re-run every suite that had failures or modifications:

```bash
make test-integration-<suite>   # for each modified suite
make test-service SERVICE=<name> # for each modified service
```

Everything must be green before proceeding.

### Phase 7: Save State & Report

Update `state.json`:

```json
{
  "last_run": "<today>",
  "last_commit": "<current HEAD sha>",
  "suites_run": {
    "<suite>": "<today>",
    ...for each suite that was run...
  }
}
```

Write the report to `docs/plans/test-maintenance-report.md`:

```markdown
# Test Maintenance Report — <date>

## Summary
- Integration suites run: N/5
- Service suites run: N
- Tests passed: N
- Tests fixed: N
- Tests deleted: N
- New tests written: N

## Fixes Applied
For each fix:
- **File**: path/to/test.py
- **Issue**: what was wrong
- **Fix**: what was changed (or "deleted — functionality removed")

## New Tests Added
For each new test:
- **File**: path/to/test.py
- **Covers**: what business logic this tests
- **Suite**: integration/<suite> or service/<service>

## Action Items
Issues that need attention but were not fixed:
- [ ] description (category: significant code bug / missing infrastructure / etc.)

## Coverage Gaps
Logic that should be tested but isn't yet (deferred for future runs):
- description of untested logic and suggested test location
```

### Phase 8: Commit

```bash
git add <all modified test files> <new test files> <report> .claude/skills/test-maintenance/state.json
git commit -m "test: maintenance run — N fixed, N added, N deleted"
```

Do NOT push — test maintenance commits stay local to avoid CI costs.

### Phase 9: Cleanup & Restore

Test runs build a lot of Docker images that aren't needed afterwards. Clean up and restore the dev stack:

```bash
# Bring the dev stack up first — so its images are "in use" and won't be pruned
make up

# Now prune — removes only unused images (test runner images, build cache)
docker system prune -a -f
```

Order matters: `make up` first ensures the dev stack's images are in use. Then `docker system prune -a -f` only removes the throwaway test images and build cache.

## Important Boundaries

- **Unit tests do not exist for purposes of this skill.** Never consider unit test coverage when assessing what's tested or deciding what to write. A feature "covered by unit tests" is an untested feature. Only integration and service tests count.
- **E2E tests do not exist either.** Same principle — different layer, irrelevant here.
- **Don't over-test.** Not every function needs an integration test. Focus on cross-service contracts, queue message flows, and database operations.
- **Don't fix what ain't broke.** If a test passes, don't refactor it. If code works but could be "cleaner", leave it.
- **Shared contracts are sacred.** If a test failure suggests `shared/contracts/` needs changing — STOP and flag it in the report. Don't modify contracts.

## Self-Feedback

If you encounter issues during the run, add an entry to `docs/skill-feedback.md`:

```markdown
## [test-maintenance] — <today's date>
- **Type**: infrastructure | stale-pattern | missing-fixture | coverage-gap
- **Problem**: <what went wrong>
- **Suggested fix**: <concrete change>
```
