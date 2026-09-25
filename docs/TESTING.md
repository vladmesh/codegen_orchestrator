# Test Infrastructure

## Test Layers

| Layer | Location | Dependencies | CI | Speed |
|-------|----------|-------------|-----|-------|
| **Unit** | `services/{svc}/tests/unit/`, `shared/tests/`, `packages/*/tests/unit/` | None (mocks) | Pre-push + CI | ~12s (parallel) |
| **Service** | `services/{svc}/tests/service/` | Docker (single service) | CI | ~5-10 min |
| **Integration** | `tests/integration/{backend,template,infra,frontend}/` | Docker Compose (full stack) | CI when relevant paths change | ~10-30 min |
| **Live** | `tests/live/` | Full stack (real services, no LLM) | Manual | ~30s–10 min |
| **E2E** | `tests/live/`, `.github/workflows/stand-e2e.yml` | Stand + real LLM | Manual workflow | up to each suite's own cap (below) |

## Running Tests

```bash
# Unit (fast, no deps — run before every push)
make test-unit                 # All services (parallel, ~12s; no host services)

# Serial mode (verbose output per service)
uv run bash scripts/test-unit-local.sh --serial

# Same suite as one module entry point (what Secretary's `check broad --module shared` runs)
uv run python -m shared            # accepts the same flags, e.g. `--serial`

# Service (Docker, single service)
make test-service SERVICE=api

# Integration (Docker Compose, full stack)
make test-integration          # All (auto-discovers tests/compose/integration/*.yml)
make test-integration-backend  # Backend tests without nested Docker
make test-integration-backend-dind  # Worker-container tests; CI runs these on pushes to main

# Live pipeline (real services, no LLM — structured 3-tier)
make test-live-smoke           # Scaffold phase only (~30s)
make test-live-engineering     # Scaffold + engineering (~3.5 min)
make test-live-mega-noop       # Free full pipeline with deploy and deterministic QA

# E2E
make stand-run SUITE=mega-live WORKER=claude QA=codex  # Level 2: the level-1 lifecycle with a real developer and QA executor
make stand-run SUITE=mega-brief  # Confirmed Product Brief through Architect, engineering, deploy and QA
make stand-run SUITE=mega-brief-package  # The same brief path onto the kit package route

# Cleanup
make test-clean                # Remove all test containers/volumes
```

## Pre-push Hook

Runs automatically before `git push`:
1. `make lint` — ruff check
2. `make test-unit` — all unit tests

Both must pass.

## Test Coverage by Service

| Service | Unit | Service | Integration | E2E |
|---------|------|---------|-------------|-----|
| api | 6 files | 2 files | via backend suite | via stand mega suites |
| langgraph | 20+ files | 3 files (engineering, reject flow, PO tools) | 3 tests (engineering worker flow) + po-tools suite (9 tests) | via stand mega suites |
| worker-manager | 15 files | — | 4 tests (worker creation/execution) | via stand mega suites |
| scheduler | 30+ files | 4 files | infrastructure stack | via stand mega |
| telegram_bot | 3 files | — | via frontend suite | — |
| infra-service | — | — | 1 file (Ansible) | — |
| scaffolder | 67 tests | — | — | — |
| shared | 9 files | — | — | — |
| packages (worker-wrapper) | 9 files | — | 3 files | — |

`make test-unit` runs the suites listed in `ALL_SUITES` (`scripts/test-unit-local.sh`).

## The CI gate covers the tree, not a list

`scripts/check-ci-gate.py` (`make ci-contract`, and the `CI Contract` job) derives the set of
test directories by walking the tree for both default pytest file patterns, then compares it
with what the CI targets actually claim: the `ALL_SUITES` table, and the pytest arguments in
`tests/compose/service/*.yml` and `tests/compose/integration/*.yml`. A directory nobody runs fails
the gate.

Two rules keep the claim honest:

- a directory argument claims the directory and everything under it; a **file** argument claims
  only that file, because pytest does not recurse from one;
- a target whose image reference is interpolated (`image: ${SOMETHING}`) is an unreadable route
  and fails the gate rather than passing as declared.

Skipping a suite stays possible, but only as a decision on the record: `UNCLAIMED_TEST_DIRS`
holds one line per skipped directory, each with a reason, and the gate fails if a listed
directory no longer exists. It currently holds three entries: `services/langgraph/tests/e2e`
(needs a real LLM key and would only ever report a skip),
`services/infra-service/tests/integration` (red) and `tests/integration/worker_wrapper` (red,
needs a checkout that exists only inside a worker container).

## Service images install exactly their lock

`service-image-imports` builds every Python service image once and, in the same image, imports its
entry modules and compares its installed distributions with its `requirements.lock`: any added,
missing or re-versioned package, or a lock that no longer satisfies its `pyproject.toml`, fails the
job and so the Required CI Gate. `make test-unit` carries the cheap half (each lock pins every
direct requirement, `make lock-deps` covers every lock, the lock is installed before any source).
The frontends' `npm ci` enforces their `package-lock.json` already. The rule and the check are
described in [DEPLOY.md](DEPLOY.md#service-images-are-a-release-too).

## What a CI run runs, and in what order

`detect-changes` runs `dorny/paths-filter` and hands the names of the filters that matched to
`scripts/ci_plan.py`, which plans the docker jobs:

- **The test matrices are the plan's legs.** `test-service` and `test-integration` build their matrix
  from the `service-legs` and `integration-legs` outputs (JSON lists). A leg nothing in the change
  reaches is not created, so it takes no runner; with no leg at all the job is skipped. The tables in
  `ci_plan.py` say which filter reaches which leg: a change to `shared/`, `packages/`, the test harness,
  CI or the dependency set reaches every leg, a one-service change only the legs whose compose files
  build that service. `services/worker-broker/**` reaches the worker-manager leg (its stack starts the
  broker) and `services/scaffolder/**` the template suite.
- **`service-image-imports` runs when an image can have changed**: the `service-images` filter (every
  Python service directory, not the frontends, plus `shared/`, `packages/`, `.dockerignore`,
  `docker-compose*.yml` and the check's own scripts), `deps` or `ci`. On every push to main it runs
  whatever changed, so every commit the release chain publishes had its entrypoints imported and its
  locks checked. A `workflow_dispatch` runs every leg and the import check.
- **The gate accepts those skips only from the plan.** `merge-gate` passes a skipped `test-service`,
  `test-integration` or `service-image-imports` only when the plan says it planned nothing for it
  (`[]`, or `false`); a skip for any other reason (lint went red, the plan is missing) fails it.

The docker jobs (`service-image-imports`, both test matrices, `template-compatibility` and the DinD
suite) wait for `lint` (Ruff format and lint, about 15 s) and `CI Contract`, not for the unit suite.
`fast-checks` runs the unit suite, the offline live regressions and the Redis regression beside them
from the start of the run, and `merge-gate` still requires it; a lint failure still stops the heavy
fan-out before any image is built.

A pull request keeps one run: a newer push to its branch cancels the older run. Every other run, a
push to main above all, is a concurrency group of its own and is never cancelled or replaced, so every
merge commit runs to its gate and gets its service and worker release.

### Docker layer cache

Every image a CI job builds reads and writes the buildx layer cache of its **Dockerfile**, in the
GitHub Actions cache (`type=gha`, scope `buildx-<dockerfile path>`, `scripts/ci_build_cache.py`). The
scope is the image definition, not the job: `services/api/Dockerfile` is built by two service legs,
four integration legs, the DinD suite and the import check, and all of them share one cache, so the
second build of an unchanged Dockerfile is `CACHED` from its first `COPY` of changed sources on.

- The test jobs expose the Actions runtime token with `crazy-max/ghaction-github-runtime` (a `run:`
  step does not get it), write a compose override with `cache_from`/`cache_to` for every service the
  compose file builds (`ci_build_cache.py compose-override`), and hand it to `make` as
  `TEST_COMPOSE_OVERRIDE`, which the test targets merge into every compose call. Locally the variable
  is unset and nothing changes.
- `service-image-imports` builds the eight Python service images in one `docker buildx bake`, in
  parallel and with the same scopes (`check_service_image_imports.py --layer-cache gha`), then runs the
  import and lock checks in the built images in parallel.
- The cache is written with `mode=max`, because the builder stages hold the slow dependency layers,
  and `ignore-error=true`, because a throttled cache service must cost speed, not a red build.
- The repository cache holds 10 GB and evicts the least recently used entries past it. Layers are
  content-addressed, so the base and apt layers the Dockerfiles share are stored once; a pull request
  reads main's cache and uploads only the layers its change produced, into its own ref.
- A test compose file declares `build:` once per image; the other services that run the same image
  name it with `pull_policy: never`, so Compose neither builds it twice nor asks Docker Hub for it.

The worker chain (`build-worker-images`) is not built through this cache: it is keyed by content and
built only when its source hash has no release (`infra/scripts/publish-worker-images.sh`).

## CI infrastructure failures

A CI job can fail because a download or a registry did not answer, not because of the code.
`.github/workflows/ci.yml` retries those downloads, bounds its docker steps in time, and when the
retries are exhausted or a bound is hit it names the failure with one line:

```
CI-INFRA-FAILURE: job=<job> step=<step> cause=<cause>
```

- `job` is the job id, with the matrix leg for a matrix job (`test-service/api`,
  `test-integration/po-tools`, `template-compatibility/baseline`).
- `step` and `cause` come from the table below. Every field is one word of `[A-Za-z0-9._/-]`,
  so `CI-INFRA-FAILURE: job=[A-Za-z0-9._/-]+ step=[A-Za-z0-9._/-]+ cause=[A-Za-z0-9._/-]+`
  matches every marker and nothing else.

`scripts/ci-infra.sh` is the only writer. It puts the marker in three places in the failing job: an
`::error title=CI infrastructure failure::` annotation (so it is also in the job log, as
`##[error]CI-INFRA-FAILURE: ...`), a line of its own in `$GITHUB_STEP_SUMMARY`, and the job output
`infra-marker`, or `infra-marker-<leg>` for each leg of a matrix job. The `Required CI Gate` reads every
job output of its `needs` through `toJSON(needs)` and repeats each marker, once, as an annotation in its
log and as a line in its own summary, so a reader of the gate alone sees it.

| Job | Step | Cause | What was retried |
|-----|------|-------|------------------|
| `lint`, `fast-checks`, `ci-contract` | `install-uv` | `uv-download`, `uv-download-timeout` | `pip install uv`, 3 attempts of at most 60 s each, 10 s then 20 s apart |
| `fast-checks` | `redis-pull` | `image-pull`, `image-pull-timeout` | `docker pull` of the Redis image the cleanup regression runs, 3 attempts of at most 90 s each |
| `test-integration/template`, `template-compatibility/<entry>` | `setup-uv` | `uv-download` | `astral-sh/setup-uv`, 3 attempts (`.github/actions/setup-uv-with-retry`) |
| `service-image-imports`, `test-service/<leg>`, `test-integration/<leg>`, `test-backend-dind-integration`, `build-service-images` | `setup-buildx` | `buildx-registry`, `buildx-registry-timeout` | creating and booting a docker-container Buildx builder, which pulls `moby/buildkit`: 3 attempts of at most 120 s each (`.github/actions/setup-buildx-with-retry`) |
| `test-service/<leg>`, `test-integration/<leg>`, `test-backend-dind-integration` | `pull-images` | `image-pull`, `image-pull-timeout` | `docker pull` of every image the suite's compose file runs without building it, 3 attempts of at most 90 s per image, before the tests start |
| `build-worker-images`, `test-backend-dind-integration` | `build-candidates`, `integration-tests` | `claude-installer-fetch` | the Claude installer fetch in `worker-base-claude/Dockerfile` (curl, 3 retries); on exhaustion the build prints `CI-INFRA-CAUSE=claude-installer-fetch` and `ci-infra.sh watch` maps that line to the marker. In CI only `build-worker-images` builds the chain; the DinD suite pulls it, and keeps the watch for a local run that builds |
| `fast-checks`, `service-image-imports`, `test-service/<leg>`, `test-integration/<leg>`, `template-compatibility/<entry>`, `test-backend-dind-integration`, `build-worker-images`, `publish-worker-images`, `build-service-images`, `publish-service-release` | `redis-cleanup`, `service-image-imports`, `service-tests`, `integration-tests`, `compatibility-smoke`, `publish`, `build-candidates` | `step-timeout` | nothing is retried: the docker step ran past its `ci-infra.sh bound` (see "Time bounds") and was stopped |

A cause ending in `-timeout` means the last attempt did not fail but hung until its bound stopped
it; a hung attempt is a failed attempt, and the next one starts after it. Only the bound's own timer
names a timeout: a command that exits 124 or 137 by itself before its bound (the statuses coreutils
`timeout` uses) is a plain failure with that status.

**A job after the gate reports its marker on itself.** `publish-worker-images` runs after the
`Required CI Gate` (it `needs: merge-gate`), so the gate can never repeat its marker. It writes the
`step-timeout` marker of its `publish` step into its own annotations and job summary, and its own
`always()` expose step hands it to the job output `infra-marker`, like every other job. Read a failed
release there, not in the gate. `build-worker-images`, which builds the chain beside the suites, is
outside the gate's `needs` and does the same with the `step-timeout` or `claude-installer-fetch`
marker of its `build-candidates` step; a failure there also skips the DinD suite, which the gate
reports.

The service image release has the same shape: `publish-service-release` runs after the gate, and
`build-service-images` is push-to-main only and outside the gate's `needs` (a pull request must not wait
for it), so neither marker is repeated by the gate. Both write it on their own job, under the output
`infra-marker`: `step-timeout` of `build-candidates` or `publish`, or `buildx-registry` of the builder.

What the marker never does:

- **It never passes the gate.** The gate's verdict comes from the `needs` results alone; the markers
  are only repeated. A job that failed on infrastructure is still a failed required job.
- **It is never written for a product failure.** A failing test, lint or build after the downloads
  succeeded writes no marker and fails exactly as before. A compose file that does not parse, an image
  build that fails for any reason but the installer fetch, and a failure before the job has checked out
  the repository (a GitHub Actions outage at `Set up job`) write none either: those stay plain failures.
  The one marker that can stand next to a product defect is `step-timeout`: it says a bound was hit,
  and a hung registry pull is its usual cause, but a test that hangs hits the same bound. Read the
  step log before rerunning.

### Time bounds

Every job in `ci.yml` has a `timeout-minutes`, so none of them waits for GitHub's 360-minute
default, and every step that builds, pulls or runs docker images has a bound of its own. The step
bound is what names a hang: `ci-infra.sh bound` runs the step under coreutils `timeout`, stops it with
its whole process group when the bound passes, and writes the `step-timeout` marker; a Buildx
bootstrap and an image pull are bounded per attempt inside `ci-infra.sh retry`.

A step bound shorter than its job is not enough: when earlier steps used their retries, a later
bound could still start too late to fire before the job limit, and the job limit stops everything,
the expose step included, without a marker. So the job limit covers the worst case of everything
that can run before the expose step: every step up to the job's last bounded step, each at its bound
(a retry at three attempts of its per-attempt bound, each with `timeout`'s 30 s kill-after, plus
10 s and 20 s of backoff; a bound plus its kill-after; any other step at its own step-level
`timeout-minutes`), plus the `always()` steps after it, plus a 2-minute margin for set-up, post
steps and the expose step. The CI contract (`scripts/check-ci-gate.py`) computes that sum per job
and matrix leg, from the constants in `scripts/ci-infra.sh` and the images each compose file pulls,
and refuses a job whose sum exceeds its `timeout-minutes`, a step inside the sum without a bound, a
job without `timeout-minutes` or above 60 minutes, and a listed docker step outside `ci-infra.sh
bound`. A step whose GitHub `timeout-minutes` fires is a plain failure, with no marker: those bound
the non-docker steps (checkout, Python and uv setup, the unit tests), whose hang is not the
registry's.

The bounds are the green runs of 2026-08-11..2026-09-23 (202 runs; per-step durations from the 40
runs up to 2026-09-23) with margin. A retry costs 3 × (attempt + 30 s) + 30 s: 8 minutes for a
Buildx bootstrap (120 s attempts), 6.5 minutes per image pulled (90 s attempts), 5 minutes for
`pip install uv` (60 s attempts).

| Job | Longest measured | Worst case before expose | Job bound | Docker step bounds |
|-----|------------------|--------------------------|-----------|--------------------|
| `detect-changes` | 0.1 min | — | 5 | — |
| `lint` | new (Ruff took 8 s inside `fast-checks`) | 11 | 15 | — |
| `fast-checks` | 3.5 min (unit tests 2.9) | 40 | 45 | Redis pull 3 × 90 s; Redis regression 3 min |
| `ci-contract` | 0.8 min | 11 | 15 | — |
| `service-image-imports` | 6.6 min (import step 6.0) | 32.5 | 35 | Buildx 3 × 120 s; imports 15 min |
| `test-service/<leg>` | 8.7 min (tests 8.4) | 50 (`scheduler`, 3 images) | 55 | Buildx 3 × 120 s; pulls 3 × 90 s per image; tests 15 min |
| `test-integration/<leg>` | 4.2 min (tests 3.9) | 40.5 (2 images) | 45 | Buildx 3 × 120 s; pulls 3 × 90 s per image; tests 10 min |
| `template-compatibility/<entry>` | 3.7 min (smoke 3.5) | 24.5 | 30 | smoke 15 min |
| `web-checks/<app>` | 0.5 min | — | 10 | — |
| `test-backend-dind-integration` | 8.5 min (suite 8.1, of which about 131 s built the worker chain; it now pulls the candidates) | 52 (3 images) | 55 | Buildx 3 × 120 s; pulls 3 × 90 s per image; suite 15 min |
| `build-worker-images` | new, not yet measured (the chain built and pushed in 2.5–4 min in the old publish job; a released hash only re-verifies) | 19.5 | 20 | candidates 15 min |
| `merge-gate` | 0.6 min | — | 5 | — |
| `publish-worker-images` | new, not yet measured (markers only; it built the chain before, 4.0 min) | 14.5 | 15 | publish 10 min |
| `build-service-images` | new, not yet measured (the 8 Python images build in 6 min in `service-image-imports`) | 37.5 | 40 | Buildx 3 × 120 s; candidates 25 min |
| `publish-service-release` | new, not yet measured | 19.5 | 20 | publish 15 min |

The non-docker steps inside a sum carry step bounds of 1–10 minutes against measured maxima of
seconds: checkout 2 (measured 6 s), Python setup 2 (1 s), `uv sync` 5 (4 s), the unit tests 10
(173 s), the offline live regressions 3 (23 s), uv setup 3 (4 s), Ruff and the other local checks 1,
the Actions runtime and the cache override 1 each, the always() assert 1, container cleanup and
artifact upload 2 (2 s).

A Buildx bootstrap took at most 0.6 minutes and a whole pull step under 0.3 minutes. Buildx is not
bootstrapped by `docker/setup-buildx-action` any more: a composite action step takes no
`timeout-minutes`, and buildx pulls the builder image on every bootstrap even when it is already
local, so neither a step bound nor a pre-pull could stop the pull that hung for 23 minutes in run
33310621862. The `workflow_dispatch` inputs `simulate_first_attempt_registry_failure` and
`simulate_first_attempt_pull_hang` make the first Buildx attempt fail or hang, to watch the retry and
the bound on a real runner; a push or pull_request run passes them empty, which means not requested.

`stand-e2e.yml` bounds its docker steps too: bring-up 30 minutes (its service release join is
bounded at 20, the third-party pull at 10), target registration and provisioning 30 (1.7–6.2; above
the provisioning wait's own 25-minute deadline, so that wait still reports), the worker release join
25 (a join bounded at 20 over a pull measured 0.8–18.7 when it ran in the foreground).

**A docker step that still hangs.** Cancelling is cooperative: `gh run cancel` waits for the step to
react, and a step blocked inside a docker pull can stay `in_progress` long after it. Force the
cancel, then rerun only what failed:

```bash
gh api -X POST repos/{owner}/{repo}/actions/runs/<run-id>/force-cancel
gh run rerun <run-id> --failed
```

Every third-party action `ci.yml` reaches, including through a local action, is pinned to a 40-character
commit SHA with the tag it was resolved from as a `# vX` comment; the CI contract refuses anything else.

## E2E Testing

Paid E2E tests are not part of required PR CI. Run the canonical named suites through the
`stand-e2e` workflow or `make stand-run SUITE=<suite>` against the isolated stand. `mega-noop`
exercises the full pipeline without a model call; `mega-live` runs the very same lifecycle — the
same class and its 39 tests — with one selected real developer and one selected real QA executor;
`mega-brief` proves the confirmed Product Brief through Architect, engineering, deploy and QA with
one selected pair. Its productive work stops at 50 minutes, then its fixture gets a separate
10-minute evidence-and-cleanup grace. `mega-brief-package` is the same path on a brief whose
capability is a one-time reminder, so the Architect plans a kit package, the worker installs it
with the kit recipe, and central QA judges the package behaviour on the route its criterion
names; it gets 65 productive minutes and a 15-minute grace.

**Every live test has its own bound.** Every test under `tests/live` runs under a `pytest-timeout`
bound with method `signal`, so a hung test fails as a pytest timeout naming the test, with a
traceback, and its fixture's cleanup still runs. The bounds come from `shared/stand_deadlines.py`.
The item that sets up the level-1 lifecycle gets the lifecycle's explicit waits: 8440 s under
`mega-noop` and 14980 s under `mega-live`. Every teardown gets the 700-second reserve, and every
other item gets 1800 s. A hang is reported first by the test timeout, then by `stand_run`'s suite
backstop (SIGINT, then a process-group kill), and last by the workflow's job limit. The ledger checks
that ordering at import for both level-1 suites. `tests/live/README.md` has the table.

**The stand runs the tested release.** `stand-e2e` builds nothing on the stand. Before any machine
is created it waits, at most ten minutes, for the worker and the service release of the workflow SHA
(`scripts/wait_release.py --chain worker --chain service`, the deploy's own wait); a revision whose
post-merge CI never publishes both is refused with retry guidance. As soon as the control-plane host
is bootstrapped, three detached jobs start there in parallel (`scripts/stand_background.sh`): the
service release pull (`pull-service-images.sh`), the worker release pull restricted to
`worker-base-common`, `-claude` and `-codex` (`WORKER_IMAGE_SUBSET`: the marker is still verified
whole; no stand suite runs `worker-base-factory`), and `uv sync --frozen` for the suite environment.
Each later step joins what it consumes, bounded and fail-closed — a failed, hung or vanished job
fails the step with its exit code and log tail: bring-up joins the service pull, pulls the
third-party images (`compose pull --ignore-buildable`) and starts every service from its digest
through the deploy's compose override with `--no-build --pull never`, then migrates, seeds and
recreates the schedulers in those containers; the worker pull is joined before the suite; the suite
joins its environment and runs with `UV_FROZEN=1`. The runner's own recreates (a QA executor switch)
use the same override, named by `STAND_SERVICE_RELEASE_COMPOSE`, and `up --no-build --pull never`;
without the override the runner refuses the run (`release_override_missing`) rather than let compose
build from the checkout. It likewise refuses, before preflight, a run whose final sweep lacks a variable
`clean_live_tests.sweep_requirements` names (`sweep_requirements_missing`). The run summary reports the span from Bootstrap start to all services
healthy (target five minutes) and each background job's duration.

**Template override**: `stand-e2e` takes two optional `workflow_dispatch` inputs,
`template_source` and `template_ref`, which point the live suite's scaffold at a template other
than the production pin — typically a `codegen-product-kit` release candidate being tried before
the pin moves to it. Both or neither; they also reseed the stand's own
`scheduler.service_template_source/ref`, and `scripts/system_configs.yaml` is untouched.

```bash
gh workflow run stand-e2e.yml -f suite=mega-brief -f worker=codex -f qa=claude \
  -f template_source=gh:vladmesh/codegen-product-kit \
  -f template_ref=0.5.1
```

**Reports**: Written to `docs/e2e_results/` — a local, gitignored output directory.

**Retired contour**: the legacy `tests/e2e` harness (mock Anthropic, dev environment smoke, live
smoke) and its `tests/compose/e2e/e2e.yml` were deleted — no target ran them and their mechanics no
longer matched the contracts. The two invariants only they asserted are kept as drafts in
[docs/examples/dev-env-sidecar-and-consumer-group-checks.md](examples/dev-env-sidecar-and-consumer-group-checks.md),
to be wired into the live and backend-dind suites under a separate issue.

## Live Pipeline Tests

**Live runs belong on the stand, never on production.** They create projects,
repositories, servers and deployed stacks and then delete them; production carries
real users' data. `shared/live_contour.py` enforces this rather than trusting the
operator: creating a live resource raises unless `LIVE_CONTOUR` names a contour
that owns test resources. Production's names stay readable to the sweep so residue
from before that rule can still be removed.

The stand is `LIVE_CONTOUR=stand`; `make stand-preflight`, `make stand-e2e` and
`make stand-clean` set it for you.

Structured 3-tier test suite in `tests/live/` — tests real services without LLM calls.

| Tier | Makefile target | Tests | Duration | What it covers |
|------|----------------|-------|----------|----------------|
| Scaffold | `test-live-smoke` | ~3 | ~30s | API CRUD, scaffold phase, stream routing |
| Engineering | `test-live-engineering` | ~3 | ~3.5 min | Worker spawn, task dispatch, engineering flow |
| Full (level 1) | `test-live-mega-noop` | 39 | ~20 min of suite time observed (2026-09-20), 155 min cap | Two stories on one project: confirmed Product Brief, scripted engineering, deploy, deterministic QA, undeploy |
| Full (level 2) | `test-live-mega-live` | 39 | no baseline measurement yet, 265 min cap | Both stories prove provider-reported developer and QA spend on the ledger, with settled owner reservations (stand runner only) |

**Key properties**:
- Module-scoped async fixtures share one pipeline run across tests per tier
- Auto-cleanup: GitHub repos, server containers, DB records (SQL cascade), port allocations
- Debug dump: captures context + last 30 lines of docker logs on failure
- Queue flush at fixture start prevents stale message pollution

### Production residue inventory

On the production host that normally runs the sweep, the PO can prove a retired
prefix has no residue without changing any resource:

```bash
make test-live-inventory PREFIX=<prefix>
```

The command reads database projects, GitHub repositories, every registered deploy
server, Redis capability and worker metadata, local Docker containers, and local
workspaces. It names every match and exits non-zero for either a match or an
unreadable surface.

### What level 1 proves — and what it does not

`mega-noop` (`tests/live/test_full_pipeline.py::TestFullPipeline`) is the free deterministic
lifecycle: **no model is asked anything, at any stage**. The PO document is a constant
(`tests/live/level1_brief.py`) driven through the released PO tools, the plan is admitted through
the architect's own coverage routes by the harness, the developer is the scripted `NoopRunner`, and
QA is the deterministic health-only observation. Its budget is the ledger in
`shared/stand_deadlines.py`, whose entries are the waits themselves; the 155-minute cap is derived
from them and stated in `tests/live/README.md`.

It proves, on real services and a real deployment: a user that registered itself through the
product's own front door — a Telegram id nobody has used, a promo code minted through the internal
API and redeemed by that named actor, and the engineering budget policy that redemption armed, which
every paid admission of the run is then judged against; a two-module bot product whose token is bound
through the product route; a Product Brief confirmed and frozen through `present_product_brief` /
`confirm_product_brief` / `create_story`, released only by the one admission step, and planned by
nothing but this run; two ordered scripted engineering Tasks on one reused Story worker; the
generated product's own CI, the merged deploy and the settings seed the confirmed brief asked for,
read back off the deployment; deterministic QA, the completed Story, the durable owner
notification a *bot* product's owner is owed; then **a second story on the same project** in the
workspace the first left behind, with its own corrected brief revision, its own deploy through the
PR poller and its own completion message; an explicit undeploy; and finally the two proofs the run
takes about itself — that it left nothing behind and that no story of it ever waited for a person.

It cannot catch anything that only a model does. There is no developer agent turn, so no prompt,
no instruction injection, no agent-written diff, no runner step a real agent takes (`make
test-integration`, and the one-shot compose containers it leaves behind, are a real developer's
path, not this one) and no transcript to judge. There is no Architect turn, so nothing here shows
that a real architect plans a brief, covers its requirements or publishes a usable acceptance
criterion. There is no QA executor turn, so no product behaviour is judged by a model: the QA gate
accepts `/health` answering and nothing else. It says nothing about executor selection, paid-run
admission of a model call, provider cost or transcript retention beyond the noop settlement rows it
asserts. Those are the paid suites' subject: `mega-live` for one developer and one QA executor pair
on this same lifecycle, `mega-brief` for the Architect-planned brief with a real developer and
central QA, and `mega-brief-package` for the same path onto the kit package route.
A level-1 run that is green therefore says the platform works end to end without a model — never
that the product a model would have built is good.

### What level 2 adds

`mega-live` (`make stand-run SUITE=mega-live WORKER=<agent> QA=<agent>`) is not a second suite: it is
`TestFullPipeline` again, with the developer resolved from `LIVE_WORKER_AGENT_TYPE` in one function
(`pipeline_helpers.level1_developer_agent_type`) and the stand runner as the only place that sets it.
Everything level 1 proves it proves again, and three facts change. A real developer (`claude` or
`codex`) is handed the product contract in prose — endpoints and their JSON, the settings and where
they are declared, the command and its menu, and the kit rules its own CI enforces — never a change
set, and both story branches must carry its commits; the deployed-product probes then judge what the
code does, unweakened. Every engineering Run must be decided for the requested developer, carry a
provider-reported cost and settle its reservation under the run owner's promo policy — Codex reports
no cost today, so a Codex-developed run fails that check by design. And a real QA executor judges
each story against repository criteria that are not health-only, its QA Run's persisted executor
decision naming the requested executor, with provider-reported QA spend on the ledger and a settled owner reservation. Its cap is 265 minutes, derived like level 1's from its
waits in `shared/stand_deadlines.py`. It still asks no model to write the brief or plan the story;
that is `mega-brief`'s subject.

Its QA executor judges a Telegram-bot product, so `mega-live` needs the QA account's Telethon
session. The `stand` environment must hold `TELETHON_API_ID`, `TELETHON_API_HASH` and
`TELETHON_SESSION`, and the session must be authorized as the QA identity the QA runtime's `/start`
probe expects (`shared.contracts.bot_access.QA_TEST_TELEGRAM_ID`) and able to resolve and write to
the stand product bot named by `STAND_PRODUCT_BOT_TOKEN`. The workflow proves all three on the
runner before any paid step (`scripts/stand_telethon_preflight.py`) and refuses the run as
`telethon_session_unauthorized`, `telethon_identity_mismatch` or `telethon_bot_unreachable`; the
credentials then reach qa-worker alone, through its own env file. `mega-noop` never opens the session
and renders with the values empty. `scripts/make_stand_session.py` authorizes a new stand session.

## Integration Test Architecture

The backend integration suite (`tests/compose/integration/backend.yml`) runs the API, Redis and
LangGraph paths that do not create worker containers. It runs on relevant pull requests.

`tests/compose/integration/backend-dind.yml` covers worker-container creation and execution with
Docker-in-Docker. `ci.yml` runs it as `test-backend-dind-integration` on every push to `main` and
when CI is manually dispatched for `main`; it stays out of pull requests, where a privileged
nested-daemon suite costs more than it protects. On `main`, `Required CI Gate` consumes that job
before worker images may be released, so a failed DinD run blocks the release marker for the exact
SHA it tested. In CI it builds no worker image: it pulls the candidates `build-worker-images`
resolved for the push into DinD by digest (`WORKER_BASE_IMAGE_SOURCE=candidates`), and the release
commits exactly those digests (docs/DEPLOY.md, "Worker base images are a release chain"). A local
`make test-integration-backend-dind` builds the chain from the tree inside DinD, as before.
Worker-path coverage is available through `make test-live-engineering`
(`tests/live/test_pipeline_engineering.py`). Use `make test-live-mega-noop` for the deterministic
scaffold, engineering, deploy and QA path, or a named `make stand-run SUITE=...` for model-backed
E2E. The default `make test-live` intentionally excludes pipeline tests and does not cover worker
creation.

The Docker-in-Docker suite spins up the full stack:
- **Services**: api, langgraph, engineering-worker, worker-manager
- **Infra**: PostgreSQL (tmpfs), Redis, Docker-in-Docker
- **Test runner**: pytest container on the same network

**Data seeding**: Tests create data via API endpoints (`POST /api/projects/`, `/api/tasks/`, `/api/servers/`). Factory fixtures in `conftest.py` (`seed_project`, `seed_task`, `seed_server`) handle creation and cleanup.

**External boundaries**: GitHub API and LLM APIs are NOT configured in the test environment. Tests verify the flow works through real services up to the external boundary, where it fails predictably (e.g., `GITHUB_ORG` not set).

**Shared helpers**: `wait_for_stream_message`, `wait_for_create_response`, `poll_task_status` in `conftest.py` — used by both worker and langgraph tests.

## Best Practices

1. Unit tests: fast (< 1s each), mock external deps
2. Integration tests: seed data via API, use unique IDs per test, cleanup via fixtures
3. One assertion per test where practical
4. Descriptive names: `test_user_creation_fails_without_email` not `test_user_1`

## Troubleshooting

- **Import errors**: Check `PYTHONPATH` includes `src/` (unit test runner sets this via `scripts/test-unit-local.sh`)
- **Config store calls from unit tests**: `make test-unit` deliberately points `API_BASE_URL` at
  `127.0.0.1:9`, matching CI where no API is running. Mock or inject `ConfigStore` in the test;
  do not rely on a locally running Compose stack. `Fast Checks` in CI is the authoritative verdict.
- **DB connection errors**: `make up` first, check `docker compose ps` for healthchecks
- **Stale test containers**: `make test-clean`
