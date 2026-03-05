# Plan: CI Pipeline Redesign (#4)

## Context

Integration tests run sequentially (~10 min wall-clock) across 5 compose stacks (backend, cli, template, frontend, infra). The Makefile already has individual `test-integration-*` targets auto-discovered from `docker/test/integration/*.yml`. The brainstorm recommends Option A: GH Actions matrix strategy to run all 5 in parallel (wall-clock → ~3-4 min).

Additionally: healthcheck intervals in non-DIND compose files are 5s (could be 2s), and Docker layer caching should be per-suite.

Brainstorms: `docs/brainstorms/ci-pipeline-redesign.md`, `docs/brainstorms/ci-integration-test-speed.md`.

Current state: `ci.yml` has a single `test-integration` job that calls `make test-integration` (sequential). `test-service` already uses matrix strategy — good precedent.

## Steps

1. [ ] Convert `test-integration` job to matrix strategy
   - **Input**: `.github/workflows/ci.yml` (lines 140-171)
   - **Output**: `test-integration` job uses `matrix.suite: [backend, cli, template, frontend, infra]`, each calling `make test-integration-${{ matrix.suite }}`. Per-suite buildx cache keys. `fail-fast: false`. Cleanup step per suite.
   - **Test**: `act` or manual PR — verify 5 parallel jobs appear, each runs its suite independently. Locally: `make test-integration-cli` still works standalone.

2. [ ] Tune healthcheck intervals in non-DIND compose files
   - **Input**: `docker/test/integration/frontend.yml`, `docker/test/integration/infra.yml`, `docker/test/integration/cli.yml` (the `interval: 5s` entries)
   - **Output**: All `interval: 5s` changed to `interval: 2s` in compose files that do NOT use DIND (frontend, infra, cli). Backend keeps its current intervals (has DIND, heavier startup). Template has no healthchecks (just a test runner).
   - **Test**: `make test-integration-cli` passes with faster intervals. `make test-integration-frontend` and `make test-integration-infra` pass.

3. [ ] Add per-suite change detection (optional optimization)
   - **Input**: `ci.yml` detect-changes job outputs, matrix suite definitions
   - **Output**: Each matrix suite only runs if relevant files changed (or `shared/` changed). Map: backend→langgraph+worker-manager+api, cli→cli+packages, template→always, frontend→telegram+api, infra→scheduler+api. Skip logic via `if` condition on each matrix entry.
   - **Test**: Push a change touching only `services/api/` — verify only backend/cli/frontend/infra suites run, template is skipped. Push `shared/` change — all suites run.

4. [ ] Validate full pipeline end-to-end
   - **Input**: All changes from steps 1-3
   - **Output**: Open a test PR touching multiple areas. Verify: 5 parallel integration jobs, correct skip logic, all pass, wall-clock time < 5 min.
   - **Test**: PR CI run green. Compare wall-clock time vs previous sequential runs.
