# CI Gate Baseline

Date: 2026-07-11

## Previous workflow

The previous `.github/workflows/ci.yml` had these jobs:

| Job | Runs | Test suites | Baseline issue |
| --- | --- | --- | --- |
| `detect-changes` | every push, PR, manual run | none | `paths-filter` did not include workflow edits, Makefile, dependency roots, shared contracts as a gate-wide trigger, or test infrastructure beyond `docker/test/**` and `tests/integration/**`. |
| `fast-checks` | every push, PR, manual run | `ruff format --check .`, `ruff check .`, `make test-unit` | Required checks were not protected by a stable final gate. |
| `test-service` | after `fast-checks` | only `api`, `langgraph`, `scheduler` service compose suites | `telegram_bot`, `worker-manager`, and `infra` service compose suites existed but were not in the matrix. If a matrix entry was not selected by paths, the test command was skipped and the job still ended green. |
| `test-integration` | only `workflow_dispatch` or PR label `run-integration-tests` | `backend`, `template`, `frontend`, `infra`, `po-tools` | Ordinary PRs did not run deterministic integration coverage for changed areas. Matrix entries with `should_run=false` skipped the test command and still ended green. |

Existing local test entry points:

- `make test-unit` runs `scripts/test-unit-local.sh`, covering unit tests for `api`, `langgraph`, `telegram_bot`, `scheduler`, `worker-manager`, `infra-service`, `worker-wrapper`, and `shared`.
- `make test-service SERVICE=<name>` runs one Docker service compose suite from `docker/test/service/<name>.yml`.
- `make test-integration-<suite>` runs one Docker integration compose suite from `docker/test/integration/<suite>.yml`.
- `make test-integration` runs all discovered integration compose suites.

GitHub branch protection on `main` had `enforce_admins` enabled and force-push/deletion disabled, but did not require status checks. A PR could be merged without waiting for CI.

## New gate model

The workflow now has one stable required status check: `Required CI Gate`.

Always required on PRs:

- `detect-changes`
- `fast-checks`: `ruff format --check .`, `ruff check .`, `make test-unit`
- `ci-contract`: `make ci-contract`
- `test-service` matrix
- `test-integration` matrix

Path-routed checks:

- Service matrix covers `api`, `langgraph`, `scheduler`, `telegram_bot`, `worker-manager`, and `infra`.
- Integration matrix covers `backend`, `template`, `frontend`, `infra`, and `po-tools`.
- `backend` integration stays `workflow_dispatch` only because it exercises DIND worker containers and coding-agent worker boundaries. It is preserved for manual full-matrix runs, but it is not a deterministic PR merge-gate suite.
- Shared code, packages, workflow edits, Makefile edits, dependency root changes, Docker test infrastructure, and integration test changes trigger the broad affected matrices instead of a single service subset.

Skipped-command guard:

- A matrix item may say it is not applicable to a PR.
- If a matrix item is applicable, the test step has a stable `id` and a following `always()` assertion verifies that the test step outcome is `success`.
- A required suite cannot become green because the actual test command was skipped.

Manual coverage:

- `workflow_dispatch` still runs the full service and Docker integration matrices.
- Live and e2e scenarios that require real LLMs, GitHub projects, VPS provisioning, Ansible deploys, or Telegram remain outside the required merge gate.
