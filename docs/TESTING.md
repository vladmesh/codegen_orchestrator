# Test Infrastructure

## Test Layers

| Layer | Location | Dependencies | CI | Speed |
|-------|----------|-------------|-----|-------|
| **Unit** | `services/{svc}/tests/unit/`, `shared/tests/`, `packages/*/tests/unit/` | None (mocks) | Pre-push + CI | ~12s (parallel) |
| **Service** | `services/{svc}/tests/service/` | Docker (single service) | CI | ~5-10 min |
| **Integration** | `tests/integration/{backend,template,infra,frontend}/` | Docker Compose (full stack) | CI when relevant paths change | ~10-30 min |
| **Live** | `tests/live/` | Full stack (real services, no LLM) | Manual | ~30s–10 min |
| **E2E** | `tests/live/`, `.github/workflows/stand-e2e.yml` | Stand + real LLM | Manual workflow | 10-60 min |

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
make stand-run SUITE=mega-llm    # Full stand pipeline with real coding and QA agents
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
| `fast-checks`, `ci-contract` | `install-uv` | `uv-download`, `uv-download-timeout` | `pip install uv`, 3 attempts of at most 60 s each, 10 s then 20 s apart |
| `fast-checks` | `redis-pull` | `image-pull`, `image-pull-timeout` | `docker pull` of the Redis image the cleanup regression runs, 3 attempts of at most 90 s each |
| `test-integration/template`, `template-compatibility/<entry>` | `setup-uv` | `uv-download` | `astral-sh/setup-uv`, 3 attempts (`.github/actions/setup-uv-with-retry`) |
| `service-image-imports`, `test-service/<leg>`, `test-integration/<leg>`, `test-backend-dind-integration`, `build-service-images` | `setup-buildx` | `buildx-registry`, `buildx-registry-timeout` | creating and booting a docker-container Buildx builder, which pulls `moby/buildkit`: 3 attempts of at most 120 s each (`.github/actions/setup-buildx-with-retry`) |
| `test-service/<leg>`, `test-integration/<leg>`, `test-backend-dind-integration` | `pull-images` | `image-pull`, `image-pull-timeout` | `docker pull` of every image the suite's compose file runs without building it, 3 attempts of at most 90 s per image, before the tests start |
| `test-backend-dind-integration` | `integration-tests` | `claude-installer-fetch` | the Claude installer fetch in `worker-base-claude/Dockerfile` (curl, 3 retries); on exhaustion the build prints `CI-INFRA-CAUSE=claude-installer-fetch` and `ci-infra.sh watch` maps that line to the marker |
| `fast-checks`, `service-image-imports`, `test-service/<leg>`, `test-integration/<leg>`, `template-compatibility/<entry>`, `test-backend-dind-integration`, `publish-worker-images`, `build-service-images`, `publish-service-release` | `redis-cleanup`, `service-image-imports`, `service-tests`, `integration-tests`, `compatibility-smoke`, `publish`, `build-candidates` | `step-timeout` | nothing is retried: the docker step ran past its `ci-infra.sh bound` (see "Time bounds") and was stopped |

A cause ending in `-timeout` means the last attempt did not fail but hung until its bound stopped
it; a hung attempt is a failed attempt, and the next one starts after it. Only the bound's own timer
names a timeout: a command that exits 124 or 137 by itself before its bound (the statuses coreutils
`timeout` uses) is a plain failure with that status.

**A job after the gate reports its marker on itself.** `publish-worker-images` runs after the
`Required CI Gate` (it `needs: merge-gate`), so the gate can never repeat its marker. It writes the
`step-timeout` marker of its `publish` step, or the `claude-installer-fetch` marker of the worker image
it builds, into its own annotations and job summary, and its own `always()` expose step hands it to
the job output `infra-marker`, like every other job. Read a failed release there, not in the gate.

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
| `fast-checks` | 3.5 min (unit tests 2.9) | 42 | 45 | Redis pull 3 × 90 s; Redis regression 3 min |
| `ci-contract` | 0.8 min | 11 | 15 | — |
| `service-image-imports` | 6.6 min (import step 6.0) | 31.5 | 35 | Buildx 3 × 120 s; imports 15 min |
| `test-service/<leg>` | 8.7 min (tests 8.4) | 48 (`scheduler`, 3 images) | 50 | Buildx 3 × 120 s; pulls 3 × 90 s per image; tests 15 min |
| `test-integration/<leg>` | 4.2 min (tests 3.9) | 38.5 (2 images) | 40 | Buildx 3 × 120 s; pulls 3 × 90 s per image; tests 10 min |
| `template-compatibility/<entry>` | 3.7 min (smoke 3.5) | 24.5 | 30 | smoke 15 min |
| `web-checks/<app>` | 0.5 min | — | 10 | — |
| `test-backend-dind-integration` | 8.5 min (suite 8.1) | 50 (3 images) | 50 | Buildx 3 × 120 s; pulls 3 × 90 s per image; suite 15 min |
| `merge-gate` | 0.6 min | — | 5 | — |
| `publish-worker-images` | 4.0 min (publish 3.9) | 19.5 | 20 | publish 15 min |
| `build-service-images` | new, not yet measured (the 8 Python images build in 6 min in `service-image-imports`) | 37.5 | 40 | Buildx 3 × 120 s; candidates 25 min |
| `publish-service-release` | new, not yet measured | 19.5 | 20 | publish 15 min |

The non-docker steps inside a sum carry step bounds of 1–10 minutes against measured maxima of
seconds: checkout 2 (measured 6 s), Python setup 2 (1 s), `uv sync` 5 (4 s), the unit tests 10
(173 s), the offline live regressions 3 (23 s), uv setup 3 (4 s), Ruff and the other local checks 1,
the always() assert 1, container cleanup and artifact upload 2 (2 s).

A Buildx bootstrap took at most 0.6 minutes and a whole pull step under 0.3 minutes. Buildx is not
bootstrapped by `docker/setup-buildx-action` any more: a composite action step takes no
`timeout-minutes`, and buildx pulls the builder image on every bootstrap even when it is already
local, so neither a step bound nor a pre-pull could stop the pull that hung for 23 minutes in run
33310621862. The `workflow_dispatch` inputs `simulate_first_attempt_registry_failure` and
`simulate_first_attempt_pull_hang` make the first Buildx attempt fail or hang, to watch the retry and
the bound on a real runner; a push or pull_request run passes them empty, which means not requested.

`stand-e2e.yml` bounds its docker steps too: bring-up 25 minutes (measured 3–14.4 over 30 green
runs), target registration and provisioning 30 (1.7–6.2; above the provisioning wait's own
25-minute deadline, so that wait still reports), worker images 30 (0.8–18.7).

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
exercises the full pipeline without a model call, `mega-llm` selects one coding/QA agent pair,
`mega-brief` proves the confirmed Product Brief through Architect, engineering, deploy and QA with
one selected pair. Its productive work stops at 50 minutes, then its fixture gets a separate
10-minute evidence-and-cleanup grace. `mega-brief-package` is the same path on a brief whose
capability is a one-time reminder, so the Architect plans a kit package, the worker installs it
with the kit recipe, and central QA judges the package behaviour on the route its criterion
names; it gets 65 productive minutes and a 15-minute grace. `matrix` runs all supported pairs.

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
asserts. Those are the paid suites' subject: `mega-llm` for one developer and one QA executor pair,
`mega-brief` for the Architect-planned brief with a real developer and central QA,
`mega-brief-package` for the same path onto the kit package route, and `matrix` for all four pairs.
A level-1 run that is green therefore says the platform works end to end without a model — never
that the product a model would have built is good.

## Integration Test Architecture

The backend integration suite (`tests/compose/integration/backend.yml`) runs the API, Redis and
LangGraph paths that do not create worker containers. It runs on relevant pull requests.

`tests/compose/integration/backend-dind.yml` covers worker-container creation and execution with
Docker-in-Docker. `ci.yml` runs it as `test-backend-dind-integration` on every push to `main` and
when CI is manually dispatched for `main`; it stays out of pull requests, where a privileged
nested-daemon suite costs more than it protects. On `main`, `Required CI Gate` consumes that job
before worker images may be released, so a failed DinD run blocks the release marker for the exact
SHA it tested.
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
