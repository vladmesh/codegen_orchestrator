# codegen_orchestrator ↔ service-template contract audit

Task: codegen_orchestrator-418 (research). Author: worker (claude-opus).

## Repositories and versions under audit

| Repo | Path used | Commit | Notes |
|------|-----------|--------|-------|
| codegen_orchestrator | this branch `pipeline/codegen_orchestrator-418` | `a3387a0e` | HEAD includes PR #30 (Required CI Gate) and PR #28 (drop copier `--trust`). Ahead of the on-disk clone `/home/dev/projects/codegen_orchestrator` (`0f0c7d84`, PR #27). |
| service-template | `/home/dev/projects/service-template` | `27c76cef` (= `origin/main`, v0.3.0) | "Changelog for 0.3.0 (#43)". Fetched `origin/main`, HEAD equals origin/main. |

Toolchain observed on the smoke host: uv 0.11.16, copier 9.16.0, docker 29.5.2, Python 3.12.3.

The audit reflects the two repos at those commits. Where a prior dogfood finding no longer holds, it is marked CLOSED in the last section with the code that closes it.

---

## 1. Contract surface: every coupling point

Each row: what the point is, who owns the contract, whether it is a **stable interface** (deliberately published, both sides depend on it) or an **incidental** dependency on an implementation detail, how it is verified today, and what breaks if it changes.

### 1.1 Copier source, ref, and `--data` options

- **Interface.** Live scaffolder runs (`services/scaffolder/src/scaffold.py:123-130`):
  ```
  copier copy <template_repo> <workspace> \
    --data "project_name=<name>" --data "modules=<modules>" \
    --data-file <yaml with task_description> \
    --defaults --overwrite --vcs-ref=HEAD
  ```
  `template_repo` comes from the queue message (`shared/contracts/queues/scaffold.py:20`, `.../worker.py:36`), whose production default is `DEFAULT_TEMPLATE_REPO = "gh:vladmesh/service-template"` (`services/scheduler/src/tasks/scaffold_trigger.py:34,167`).
- **Consumed questions** (`copier.yml`): `project_name` (validator `^[a-z][a-z0-9_-]*$`), `modules` (comma-separated **str**, not multiselect; default `backend`; values `backend,tg_bot,notifications,frontend`), `task_description`. Other questions (`project_description`, `author_*`, `python_version`, `node_version`) fall back to defaults via `--defaults`. `project_slug = project_name | replace('-','_')` is computed.
- **Owner.** service-template owns the question set and validators; codegen owns which three it passes and the ref.
- **Stable vs incidental.** Stable interface. The three `--data` keys and the copier invocation shape are the documented bootstrap contract (`docs/plans/plan-tooling-removal.md:54`, template `infra/README.md`).
- **Verification.** No cross-repo test. Verified here by the smoke (§2) and by scaffolder unit tests that assert the command string.
- **Breakage.** Renaming/removing any of the three questions, changing the `modules` format to a list, or tightening the `project_name` validator breaks scaffold with a copier error. `--vcs-ref=HEAD` means a template change lands the next scaffold with no pin (see §3.2).

### 1.2 Post-copy setup

- **Interface.** After copier, scaffolder runs `make setup` (`scaffold.py:142`), then `git add/commit/push` (`:152-158`). There is **no copier `_tasks`**; `copier.yml` defines none. Post-copy work is entirely `make setup`. The mandated order is `copier copy` → `make setup` → other targets (`plan-tooling-removal.md:54`).
- **`make setup`** (template `Makefile.jinja:47-66`): `uv venv`; `uv pip install -e .framework/ ruff xenon`; per-service `uv sync --frozen`; backend-only `python -m framework.generate` (spec codegen); `ruff format`; `ruff check --fix` (non-fatal on failure, PR #38); `git config core.hooksPath .githooks`.
- **Owner.** service-template owns `make setup`. codegen owns the decision to call it.
- **Stable vs incidental.** Stable interface. The target name `setup` and its "one command bootstraps everything" semantics are the contract.
- **Verification.** Smoke §2 (`make setup` exit 0). No cross-repo CI.
- **Breakage.** Renaming `setup`, or making it require host tooling not present in the worker image, breaks every scaffold. A new required tool in `make setup` must exist in `worker-base-common` (§1.4).

### 1.3 Worker image and tooling

- **Interface.** Workers run `make setup`/`make lint`/`make tests` natively (no Docker) inside `worker-base-*` images. `worker-base-common/Dockerfile` provides: `git curl jq make` (`:48-53`), `uv`/`uvx` via `COPY --from=ghcr.io/astral-sh/uv:latest` (`:56`), worker-wrapper + `shared` at `/app`, non-root user `worker` (uid 1000), uv cache dir `/home/worker/.cache/uv` (`:73`). `worker-base-claude` adds the Claude CLI + ripgrep; `worker-base-factory` adds `droid`. **No `node`, no `copier`, no docker CLI baked in** (copier/ruff installed on demand via `uv tool`, per the `:55` comment).
- **Owner.** codegen owns the image; service-template implicitly dictates the required toolset through what `make setup`/`make lint`/`make tests` invoke (uv, ruff, xenon, deptry, pytest, mypy, and for frontend: node).
- **Stable vs incidental.** Incidental/implicit. There is no declared "required tools" manifest shared between the repos; the worker image must simply happen to contain everything the template's make targets call.
- **Verification.** None automated across repos. A missing tool surfaces only at runtime inside a worker.
- **Breakage.** If the template starts using a tool absent from `worker-base-common` (e.g. a `frontend` project needs `node`, which the common base lacks), `make setup`/`make lint` fails inside the worker. This is the weakest-typed point in the whole contract.

### 1.4 Workspace ownership (chown / HOST_UID)

- **Interface.** Two independent mechanisms:
  1. Runtime file ownership of the bind-mounted `/workspace`: `worker-manager` execs `chown -R worker:worker /workspace` as root right after container start (`services/worker-manager/src/manager.py:456`).
  2. Container process UID for compose services: template compose sets `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"` on backend/tg_bot/notifications (`compose.base.yml.jinja:16,42,59`); `compose_runner` injects `HOST_UID=1000`/`HOST_GID=1000` into the compose env (`compose_runner.py:216-218`).
- **Owner.** Split. codegen owns the workspace chown; service-template owns the `user:` directive and the `HOST_UID/HOST_GID` env names.
- **Stable vs incidental.** The `HOST_UID`/`HOST_GID` env names are a stable interface. The template also carried a conditional `chown` in an earlier `_tasks`, which is now gone (no `_tasks` at all); the real guarantee is `manager.py:456`. Memory note "chown-task in `_tasks` is dead code" is now moot because the task itself no longer exists.
- **Verification.** No test. Smoke ran with default 1000:1000 and produced no permission errors.
- **Breakage.** If the template dropped `user:` or renamed the env vars, compose services would run as root and write root-owned files into the bind-mounted workspace, which the `worker` user then cannot edit.

### 1.5 Make targets (lifecycle API)

- **Interface.** The generated `Makefile.jinja` is the lifecycle API both humans and the orchestrator drive. Groups:
  - Bootstrap/quality: `setup`, `lint`, `format`, `typecheck`, `tests`, `check-deps`, `tooling-tests`.
  - Worker (portless) mode: `worker-start` (`up -d --build --wait $(svc)` over base+dev), `worker-stop` (`down --remove-orphans`), `worker-clean` (`down --volumes --remove-orphans`), `down` (alias), `infra-start`, `ps`, `log`, `smoke-probe`, `worker-call`.
  - Local mode (adds host ports): `dev-start`, `dev-stop`, `dev-clean`, `dev-smoke`.
  - Prod: `prod-start`, `prod-stop`. Backend-only: `migrate`, `makemigrations`, `test-integration`, `generate-from-spec`, `openapi`, `typescript`, `validate-specs`.
- **Owner.** service-template.
- **Stable vs incidental.** The worker-mode and setup targets are a stable interface (added deliberately in v0.3.0, PRs #41/#42, documented in `infra/README.md`). The `migrate`/`makemigrations` gate (`SKIP_INFRA_START=1`) is stable.
- **Verification.** Smoke §2 exercised `setup`/`lint`/`tests`/`worker-start`/`smoke-probe`/`worker-call`/`worker-clean`.
- **Breakage.** Renaming a worker-mode target breaks the Makefile-override path (§1.6) or any direct `make worker-start` call.

### 1.6 Makefile patching (worker-wrapper override)

- **Interface.** `worker-wrapper` appends an override block to `/workspace/Makefile` (`packages/worker-wrapper/src/worker_wrapper/wrapper.py:474-511`), guarded by marker `# --- orchestrator overrides ---` (idempotent). It redefines **`dev-start`** and **`dev-stop`** to `curl` the local compose proxy:
  ```
  dev-start:  curl -sf -X POST http://localhost:9090/infra/compose -d '{"args":["up","-d","--wait","$(svc)"],"cwd":"."}'
  dev-stop:   curl -sf -X POST http://localhost:9090/infra/compose -d '{"args":["down","--remove-orphans"],"cwd":"."}'
  ```
  Make uses the last definition, so the appended recipes win.
- **Owner.** codegen (worker-wrapper).
- **Stable vs incidental.** **Incidental and now partly mismatched.** The override targets `dev-start`/`dev-stop`, but the template's *portless* targets are now `worker-start`/`worker-stop` (`dev-start` in the template is the local-ports layer). The override still "works" only because it replaces the whole recipe with a proxy call (ports never enter the picture), and because `make migrate` transitively calls a start target. This is the least principled coupling: string-appending another repo's build file and depending on target names.
- **Verification.** `packages/worker-wrapper/tests/unit/test_makefile_overrides.py` (codegen-only; does not see the template).
- **Breakage.** If the template renames `dev-start`, or a worker calls `make worker-start` expecting it to be proxied, the override misses and either fails (no Docker socket) or runs the wrong layer. Silent target drift.

### 1.7 Compose proxy API

- **Interface.** Served by worker-manager FastAPI: `POST /api/worker/{worker_id}/infra/compose` (`services/worker-manager/src/routers/compose.py:27`). Request `{args: list[str], cwd: str=".", timeout: int=120}`; response `{exit_code, stdout, stderr}`. Client side: worker-wrapper local server `POST /infra/compose` on `127.0.0.1:9090` forwards the raw body to `${WORKER_MANAGER_URL}/api/worker/${WORKER_ID}/infra/compose` (`http_server.py:169-207`, timeout 180). Workers have **no docker socket**; this handle is the only way to touch Docker.
- **Allowlist** (`compose_validator.py:6-9`): `ALLOWED_COMMANDS = {up, down, build, run, ps, logs, stop}`; blocks `-it/-i/-t/--interactive/--tty`; rejects compose files that are not mappings or that use **absolute** bind mounts. Ports are deliberately *not* blocked here (stripped by the runner instead).
- **Owner.** codegen.
- **Stable vs incidental.** Stable interface between worker-wrapper and worker-manager. It is a codegen-internal API, not a service-template contract, but it constrains what generated compose files may do (allowed subcommands, no absolute mounts).
- **Verification.** worker-manager unit tests (`test_compose_api.py`, `test_compose_validator.py`, `test_compose_runner.py`).
- **Breakage.** A generated compose file using a banned subcommand or an absolute host mount is rejected at 400.

### 1.8 Compose filenames and layers

- **Interface.** worker-manager hardcodes the default compose files it runs: `_DEFAULT_COMPOSE_FILES = ["infra/compose.base.yml", "infra/compose.dev.yml"]` (`compose_runner.py:21`, used when the caller passes no `-f`). The template ships exactly those plus `compose.local.yml`, `compose.prod.yml`, `compose.tests.integration.yml` under `template/infra/`.
- **Layering** (template `Makefile.jinja:6-13`): base = `compose.base.yml`; dev = base+`compose.dev.yml` (portless); local = dev+`compose.local.yml` (host ports); prod = base+`compose.prod.yml`.
- **Owner.** Shared. service-template owns the filenames and layer semantics; codegen hardcodes base+dev as the worker default.
- **Stable vs incidental.** Stable-by-necessity but **fragile**: the exact relative paths `infra/compose.base.yml` and `infra/compose.dev.yml` are duplicated as string literals in codegen with no shared constant. `compose_runner.py:153` comment: "All projects use service-template layout: infra/compose.base.yml + compose.dev.yml."
- **Verification.** Only the smoke and worker-manager unit tests referencing the literal paths.
- **Breakage.** Renaming or relocating either compose file in the template silently breaks the proxy default (compose errors "no configuration file").

### 1.9 Compose project name

- **Interface.** Template compose top-level `name: ${COMPOSE_PROJECT_NAME:-{{ project_slug }}}` (`compose.base.yml.jinja:8`). worker-manager always passes `--project-name worker_<worker_id>` (`compose_runner.py:122,199-210`). Note the two conventions for one worker: compose **project** = `worker_<id>`, Docker **container** = `worker-<id>` (`manager.py:74`).
- **Owner.** Shared.
- **Stable vs incidental.** Stable interface (env override + explicit `--project-name` both honored, PR #42). Passing `--project-name` explicitly is the documented isolation mechanism (`infra/README.md`).
- **Verification.** Smoke §2 used a unique `COMPOSE_PROJECT_NAME=codegen418smoke`; all artifacts were namespaced and removed cleanly.
- **Breakage.** Relying on the `project_slug` default (not passing `--project-name`) collides parallel workers of the same project. Raw `docker compose` from the project root also mis-derives the project name from `infra/`'s dir (dogfood B2, §5).

### 1.10 Env precedence

- **Interface.** Generated `.env` is the source. Template `Makefile.jinja:3-4` does `-include .env` + `export`; compose reads `env_file: ../.env` and interpolates `${VAR:-default}`. `compose_runner` passes `--env-file <workspace>/.env` when present and also merges its keys into the subprocess env so they win (`compose_runner.py:194-226`). DB URLs are assembled from parts (`POSTGRES_USER/PASSWORD/HOST/PORT/DB`) in `x-backend-env` (`compose.base.yml.jinja:2-5`); a full `DATABASE_URL` in the shell does not override the assembled one.
- **Owner.** service-template owns the `.env` schema and the compose interpolation defaults; codegen owns injecting `HOST_UID/HOST_GID` and `--env-file`.
- **Stable vs incidental.** Stable but under-specified: which keys are the stable contract (`POSTGRES_HOST/PORT` + `REDIS_URL` vs `DATABASE_URL`/`ASYNC_DATABASE_URL`) is an open question flagged in the 2026-07-10 dogfood.
- **Verification.** Smoke ran with the generated `.env` defaults (postgres/postgres/service).
- **Breakage.** Compose `${VAR:-default}` fallbacks (e.g. `${POSTGRES_HOST:-db}`) mask a missing var instead of failing fast, contrary to codegen's fail-fast rule.

### 1.11 Docker networks and DNS

- **Interface.** No compose file declares `networks:`. `compose.base.yml.jinja:10-11` states the contract in a comment: "Do not declare custom networks here. Codegen sibling workers replace the implicit default network with their pre-created dev network." worker-manager writes `.codegen-network.yml` redirecting the compose `default` network to `dev_proj_<worker_id>` (external) for `up/run/build` (`compose_runner.py:63-74,165-170`). Workers join `codegen_worker` (shared: api/redis/worker-manager) plus per-worker `dev_proj_<id>` (project sidecars), per `docs/parallel-workers.md` and `docs/brainstorms/worker-db-network-isolation.md`. Service DNS names are the API: `db:5432`, `redis:6379`, `backend:8000`.
- **Owner.** Shared. service-template owns "no custom networks"; codegen owns the override and the two-network topology.
- **Stable vs incidental.** Stable interface, explicitly documented on both sides (`template/infra/README.md`, `compose.base.yml.jinja` comment).
- **Verification.** Smoke reached `backend:8000` by DNS from an ephemeral container on the project default network.
- **Breakage.** A generated project that declares a custom network or renames `default` defeats the override and the worker cannot reach its sidecars.

### 1.12 Published ports (portless worker mode)

- **Interface.** Host ports live **only** in `compose.local.yml.jinja` (`backend 8000`, `db 5432`, `redis 6379`, `frontend 3000`). Worker mode (base+dev) publishes none. As a belt-and-braces measure worker-manager also writes `.codegen-ports.yml` with `ports: !reset []` for any service that declares ports (`compose_runner.py:24-60,181-185`).
- **Owner.** Shared.
- **Stable vs incidental.** Stable interface ("worker mode must not depend on published host ports", `infra/README.md`; PR #33 split the local layer).
- **Verification.** Smoke §2: `docker compose ... ps` showed only container-internal ports (`8000/tcp`, `5432/tcp`, `6379/tcp`), no host mappings.
- **Breakage.** Moving a `ports:` back into base/dev would collide parallel workers; the `!reset` override mitigates but should not be relied on as the primary guarantee.

### 1.13 Healthchecks and probes

- **Interface.** Compose healthchecks (`compose.base.yml.jinja`): backend `python -c urllib.request.urlopen('http://localhost:8000/health')` (5s/3s/×10/start 15s); db `pg_isready` (10s/5s/×5); redis `redis-cli ping` (5s/3s/×5). App endpoint `GET /health` → `{"status":"ok"}`. Readiness is defined by Compose health state (`up --wait`), not sleeps. Probes: `make smoke-probe` (GET only) and `make worker-call` (arbitrary method/body) run an ephemeral `run --rm --no-deps` container that hits a service by DNS.
- **Owner.** service-template.
- **Stable vs incidental.** Stable interface; `--wait` health gating is the documented readiness contract.
- **Verification.** Smoke: `worker-start ... --wait` gated on all three healthchecks; `smoke-probe`/`worker-call` returned 200.
- **Breakage.** Removing a healthcheck makes `up --wait` return before the service is ready; the orchestrator would proceed against a not-ready sidecar.

### 1.14 Cleanup

- **Interface.** Worker teardown: `delete_worker` runs `compose down -v` in the stored workspace (`manager.py:140-146`), force-removes the container, removes the `dev_proj_<id>` network, preserves the workspace dir. Periodic GC (`garbage_collector.py`): orphaned worker containers/networks (name `dev_proj_*`), stale Redis entries, workspace dirs older than ~35h, images older than 7d. Template side: `worker-clean`/`dev-clean`/`test-integration` all `down --volumes --remove-orphans`.
- **Owner.** codegen owns runtime GC; service-template owns the `*-clean` targets.
- **Stable vs incidental.** Stable interface (`--remove-orphans`, `--volumes`).
- **Verification.** Smoke §2 cleanup left zero containers/networks/volumes (§4).
- **Breakage.** A generated named volume not covered by `down --volumes` would leak; the smoke's only named volume (`db_data`) was removed by `worker-clean`.

---

## 2. Deterministic scaffold-smoke

One isolated project taken from current codegen scaffold semantics to current service-template. Run in a scratch dir outside any repo, with a unique compose project name to avoid touching other projects.

**Inputs.** service-template `27c76cef`; scaffold command shape copied verbatim from `scaffolder/src/scaffold.py`; copier `9.16.0` via `uvx`; `project_name=codegen-tpl-smoke`, `modules=backend`, a one-line `task_description`. `COMPOSE_PROJECT_NAME=codegen418smoke`.

**Steps, commands, results.**

| Step | Command | Result |
|------|---------|--------|
| Scaffold | `uvx copier@9.16.0 copy /home/dev/projects/service-template <tmp> --data project_name=codegen-tpl-smoke --data modules=backend --data-file <yaml> --defaults --overwrite --vcs-ref=HEAD` | exit 0, 3.5s. Rendered `infra/compose.*.yml`, `Makefile`, `services/backend`, `shared/spec`, `.env`. |
| Bootstrap | `make setup` | exit 0, 3.8s. venv created, `.framework/` + ruff + xenon installed, backend `uv sync --frozen`, `framework.generate` produced schemas/protocols/routers, ruff format clean. |
| Lint | `make lint` | exit 0. ruff format+check clean, xenon clean, spec validation PASSED, spec-compliance PASSED, controller-sync PASSED, deptry "No dependency issues". |
| Tests | `make tests` | exit 0. 14 backend tests passed; no tooling/unit tests present. |
| Worker mode (portless) | `make worker-start svc=backend` (base+dev) | exit 0, 54s (first build). Built `codegen_tpl_smoke-backend:latest`, created `codegen418smoke_default` net + `db_data` vol, brought up db/redis/backend, all reached **Healthy** via `up --wait`. |
| Port check | `docker compose ... ps` | Only container-internal ports (`8000/tcp`, `5432/tcp`, `6379/tcp`); **no host port publishing**. |
| Health probe | `make smoke-probe SMOKE_RUNNER=backend SMOKE_URL=http://backend:8000/health` | exit 0, `http://backend:8000/health -> 200` from an ephemeral in-network container. |
| In-network call | `make worker-call SMOKE_RUNNER=backend url=http://backend:8000/health method=GET` | exit 0, `GET ... -> 200`. |
| Cleanup | `make worker-clean` (`down --volumes --remove-orphans`) + `docker rmi` the built image | exit 0; all project containers/network/volume removed. |

**Limitations (what this smoke did not exercise).**
- It ran the template-native portless worker-mode (`make worker-start`/`smoke-probe`/`worker-call`), which is the *same* `docker compose -f base -f dev` invocation the compose proxy ultimately issues. It did **not** stand up the full codegen stack (api, redis, worker-manager, a real worker container) or drive the `POST /infra/compose` HTTP proxy end-to-end, because that needs the whole 9-service `make up` and real agent images. The proxy path is covered by worker-manager unit tests, not by this smoke.
- It did not run `git push`/GitHub (no network side effects), and did not exercise `tg_bot`/`notifications`/`frontend` modules. Frontend would additionally require `node`, which `worker-base-common` lacks (§1.3).
- copier was invoked from a local checkout, not the production default `gh:vladmesh/service-template`. Both resolve `--vcs-ref=HEAD`; the local checkout is at the same commit as `origin/main`.

---

## 3 & 6. Resource-leak check and unresolved decisions

### 3.1 Resource-leak verification

Baseline captured before the smoke (containers, non-default networks, volumes). After `make worker-clean` + image removal:

- Containers labeled `com.docker.compose.project=codegen418smoke`: **0**.
- Networks matching `codegen418smoke`: **0**.
- Volumes matching `codegen418smoke`: **0**.
- Built image `codegen_tpl_smoke-backend:latest`: removed.
- Unrelated running project `cp-kanboard` (project `board`) and all other projects' networks/volumes: untouched. No temp worktree was created (worked in scratchpad). The scratch project dir lives under the session scratchpad and is disposable.

No leaks; no foreign Docker project affected.

### 6.1 Makefile patching vs a native orchestrator API

**Fact.** worker-wrapper appends `dev-start`/`dev-stop` overrides to the generated Makefile (§1.6). The template's portless targets are `worker-start`/`worker-stop`. The override predates v0.3.0's worker-mode targets.

**Options.**
- **A. Template exposes a proxy-aware indirection.** Add a `COMPOSE_PROXY_URL` (or `DOCKER_COMPOSE` override) hook to the template so worker mode routes through the proxy without codegen editing the file. Trade-off: puts orchestrator-awareness into a general-purpose template; needs a documented env contract.
- **B. Keep patching but target the real names.** Change the override to redefine `worker-start`/`worker-stop` (and stop patching `dev-start`, which is the local-ports layer). Trade-off: smaller change, still string-appends another repo's build file.
- **C. Stop patching; call the proxy directly.** Have the worker call `POST /infra/compose` (or a thin `worker-wrapper compose ...` shim) instead of `make`, and let agents use `make` only for lint/tests. Trade-off: agents lose the familiar `make dev-start`.

**Recommendation.** B now (correctness fix, tiny), A next (removes the string-append coupling entirely). The current override's dependence on the stale `dev-start` name is a latent bug the moment anyone relies on `make worker-start` inside a worker.

### 6.2 Template version pinning

**Fact.** Production default is `template_repo = "gh:vladmesh/service-template"` (`scaffold_trigger.py:34`) with `--vcs-ref=HEAD` (`scaffold.py:128`). No tag/commit pin. Separately, the scaffolder container mounts a local checkout at `/data/service-template:ro` (`docker-compose.yml:333`) via `SERVICE_TEMPLATE_PATH`, which the default `gh:` path does **not** use. The 2026-07-11 dogfood recorded that a bare `gh:` source without `--vcs-ref=HEAD` silently resolves to the last **tag** (then 0.2.0), missing new features.

**Options.**
- **A. Pin per project.** Record the resolved template commit in the message / `.copier-answers.yml` and reuse it for that project's lifetime. Trade-off: reproducible, but projects drift from template improvements unless explicitly updated.
- **B. Stay on HEAD (current).** Always latest main. Trade-off: a template regression breaks all new scaffolds at once; no reproducibility.
- **C. Pin to the latest release tag, bump deliberately.** Trade-off: needs the template to keep tagging (it does: v0.3.0); the `gh:`-defaults-to-tag trap becomes the feature instead of a footgun.

**Recommendation.** C. Pin the orchestrator default to an explicit tag and drop the reliance on `--vcs-ref=HEAD`, so scaffolds are reproducible and template releases are adopted on purpose. Also resolve the `gh:` vs mounted-`/data/service-template` inconsistency: pick one source. **Open question:** which source is intended in production (the mount looks vestigial).

### 6.3 Tooling-removal iteration 4 and shared uv-cache

**Fact — largely CLOSED in code, stale in the plan.** `service-template/docs/plans/plan-tooling-removal.md:6,108-153` lists iteration 4 (slim worker-base image, add uv, add shared uv-cache) as "ожидает реализации на стороне оркестратора." The codegen code already implements it:
- `worker-base-common/Dockerfile:56` uses `COPY --from=ghcr.io/astral-sh/uv`; ruff/xenon/pytest/mypy/copier are **not** baked (installed on demand via `uv tool`).
- A named `uv-cache` volume is declared (`docker-compose.yml:632`) and mounted by the scaffolder (`:334`, at `/root/.cache/uv`) and by every worker (`container_config.to_volume_mounts`, at `/home/worker/.cache/uv`).

**Residual open question.** The single `uv-cache` volume is shared between a **root** process (scaffolder) at `/root/.cache/uv` and **worker uid-1000** processes at `/home/worker/.cache/uv`. uv uses hardlinks and ownership-sensitive cache files; a cache populated by root may not be writable by uid 1000 (and vice versa), which can degrade or error the "shared cache" benefit the plan wanted. This is worth a targeted check.

**Recommendation.** Mark iteration 4 DONE on the codegen side (update the plan), and verify/instrument the cross-UID uv-cache sharing before treating it as a win. If cross-UID sharing is unsafe, give scaffolder and workers separate caches or align UIDs.

### 6.4 ensure-workspace gate

**Fact.** Two gates exist. (1) Scaffolder `run_ensure_workspace` (`scaffold.py:198`): if the workspace already has non-`.git` files → skip; else clone+`make setup` if the repo exists on GitHub; else error. (2) worker-wrapper `_check_workspace_ready` refuses to launch the agent unless `/workspace/.copier-answers.yml` exists (the copier answers file, written only by a real scaffold).

**Options.**
- **A. Keep both, make the contract explicit.** Document `.copier-answers.yml` as the "workspace is scaffolded" sentinel shared across services (it already is, implicitly).
- **B. Single source of truth.** Have worker-manager check the same sentinel before creating the worker so the wrapper's refusal never triggers mid-run.

**Recommendation.** A. The `.copier-answers.yml` sentinel is a clean, template-owned marker; document it as the contract for "scaffolded" and keep both guards. Low risk, no code change strictly required. **Not a blocker.**

### 6.5 Ownership / chown responsibility

**Fact.** Covered in §1.4. Runtime ownership is guaranteed by `manager.py:456` (`chown -R worker:worker /workspace`), not by the template. The template's contribution is `user: ${HOST_UID:-1000}:${HOST_GID:-1000}` on services and build-time `chown -R 1000:1000 /app` in service Dockerfiles. The scaffolder itself runs copier as root and does not pass `HOST_UID`, so files it writes are root-owned until the worker chowns them.

**Options.**
- **A. Status quo.** worker-manager owns runtime ownership; template owns process UID. Trade-off: two mechanisms, but each is in the layer that can actually enforce it.
- **B. Make scaffolder honor HOST_UID.** Run copier under, or chown to, a configurable UID so the pushed repo isn't root-owned on disk. Trade-off: only matters for the on-disk workspace between scaffold and first worker start; the worker chown already fixes it.

**Recommendation.** A, documented. The split is deliberate and works; the memory note about a dead chown `_tasks` is obsolete because the template no longer has any `_tasks`. **No code change needed;** record the ownership responsibility in the contract (§7) so it is not "fixed" in the wrong repo later.

### 6.6 HTTP-service access inside the worker network

**Fact.** Workers reach project HTTP services by DNS on `dev_proj_<id>` (`backend:8000`), proven by the smoke's `smoke-probe`/`worker-call`. There is **no** general host-port-forward or TCP proxy; the only Docker handle is `POST /infra/compose`. The 2026-07-11 dogfood flagged **B1**: `smoke-probe` is GET-only (URL hardwired to `SMOKE_URL`, no method/body), so a worker cannot easily do an in-network POST without hand-rolling the `run --rm --no-deps` incantation. `worker-call` (added since) does support method/body, which partially closes B1.

**Options.**
- **A. Confirm `worker-call` closes B1** and document it as the in-network request tool for agents (the dogfood predates or overlaps its addition).
- **B. Add a first-class request helper** to worker-wrapper so agents don't touch compose at all for a simple in-network call.

**Recommendation.** A. `worker-call SMOKE_RUNNER=<svc> url=... method=... body=...` is present and worked in the smoke; document it in `coding-agents.md` as the supported way for a worker to call a project HTTP service. **Open question:** is DNS-on-`dev_proj_*` sufficient for all agent needs, or is a host-side proxy ever required (the 2026-07-10 dogfood left this open)? The smoke suggests DNS is sufficient for the health/probe case.

---

## 4. Minimal target contract v1

What both repos should treat as stable between them. Everything here has a single documented owner and a breakage consequence above.

**Commands (service-template owns; codegen may call):**
- `copier copy <src> <dst> --data project_name=… --data modules=… --data-file <task_description> --defaults --overwrite --vcs-ref=<ref>` — scaffold.
- `make setup` — mandatory bootstrap, run once after copier, before any other target.
- `make lint`, `make tests`, `make typecheck` — quality gates, native (no Docker).
- `make worker-start svc=<s>` / `worker-stop` / `worker-clean` — portless lifecycle (base+dev).
- `make smoke-probe` / `make worker-call` — in-network readiness / request.
- `make migrate` / `makemigrations` with `SKIP_INFRA_START=1` — DB (backend only).

**Variables:**
- copier `--data` keys: `project_name` (validator `^[a-z][a-z0-9_-]*$`), `modules` (comma-separated str), `task_description`.
- `COMPOSE_PROJECT_NAME` — set per worker; explicit `--project-name` wins.
- `HOST_UID` / `HOST_GID` — process UID for compose services (default 1000).
- `.env` DB/redis keys — declare the stable subset: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `REDIS_URL`. (Resolve DATABASE_URL-vs-parts as part of §6.2/§1.10.)

**Files:**
- `infra/compose.base.yml`, `infra/compose.dev.yml` — the two files worker mode runs; must not declare `networks:`; ports only in `compose.local.yml`.
- `.copier-answers.yml` — "workspace is scaffolded" sentinel.
- `Makefile` at repo root — the lifecycle API surface.

**Lifecycle semantics:**
- Readiness = Compose health state via `up --wait` (db `pg_isready`, redis `ping`, backend `GET /health`).
- Networking = implicit `default` network, redirected by the orchestrator to `dev_proj_<id>`; DNS names `db:5432`, `redis:6379`, `backend:8000`.
- Ownership = orchestrator chowns `/workspace` at worker start; template sets service process UID via `HOST_UID/HOST_GID`.
- Cleanup = `down --volumes --remove-orphans` removes all project containers/network/volumes.

Anything not in this list (exact `_exclude` globs, framework internals, `dev-start`/local ports, `xenon` thresholds, image tags) is an implementation detail either repo may change without coordination.

---

## 5. Reconciliation with current state (closed vs still-open)

| Prior finding (source) | Status | Evidence |
|---|---|---|
| scaffold calls copier with `--trust` (memory id 11) | **CLOSED** | PR #28 "Drop copier trust flag"; `scaffold.py:123-130` and `scaffold_phase.py:65-69` pass no `--trust`. Template moved module selection to `_exclude` (v0.3.0 #36), so `--trust` is unneeded. |
| chown `_tasks` in template is dead code (memory id 10/11) | **OBSOLETE** | Template has **no `_tasks`** at all now. Runtime ownership is `manager.py:456`. The observation is moot. |
| Iteration 4 (slim worker image + uv + shared uv-cache) awaits orchestrator work (`plan-tooling-removal.md`) | **CLOSED in code / stale in plan** | `worker-base-common/Dockerfile:56` (uv, no baked tooling), `docker-compose.yml:632` + `:334` + `container_config` (shared `uv-cache`). Residual cross-UID cache question in §6.3. |
| No worker-mode targets; ports always published (pre-v0.3.0 dogfood) | **CLOSED** | v0.3.0 added `worker-start/stop/clean`, `smoke-probe`, `worker-call`; ports moved to `compose.local.yml` (#33). Smoke confirms zero host ports. |
| DB DNS collision: worker resolves `db` to orchestrator postgres (`worker-db-network-isolation.md`) | **CLOSED (Phase 1)** | Separate `codegen_worker` network; `project-db` alias and `_patch_db_hostname` removed; marked done #22. |
| Bootstrap needs `--trust` / raw `docker compose` works from root | **CLOSED / CONFIRMED-OPEN** | Bootstrap without `--trust` confirmed (smoke + 2026-07-11 dogfood). Raw `docker compose` from project root still mis-derives project name (dogfood B2) — real, still open; mitigated only by always going through `make`/proxy. |
| `smoke-probe` GET-only, no in-network POST (dogfood B1) | **PARTIALLY CLOSED** | `make worker-call` now supports method/body; verified GET in smoke. Document it (§6.6). |
| Makefile override targets `dev-start`/`dev-stop` | **STILL OPEN (drift)** | Template's portless targets are `worker-start`/`worker-stop`; override still names the local-layer `dev-start` (§6.1). |
| Minor: `infra/compose.frontend.yml` referenced but absent (template) | **OPEN (template-side nit)** | `services.yml.jinja` and frontend README reference a non-existent file. |

---

## Proposed code-cards (2–4, small, verifiable)

Sequenced; each has clear boundaries and a checkable result. No umbrella refactor.

1. **Fix the Makefile override target drift (codegen).** In `worker-wrapper/wrapper.py:_inject_makefile_overrides`, redefine `worker-start`/`worker-stop` (the template's portless targets) instead of `dev-start`/`dev-stop`; keep the proxy body. Update `test_makefile_overrides.py`. *Result:* `make worker-start`/`worker-stop` inside a worker route through the proxy; a unit test asserts the new target names. Depends on nothing. Small.

2. **Pin the template version (codegen).** Replace the `--vcs-ref=HEAD` default with a configurable, explicit tag (e.g. `SERVICE_TEMPLATE_REF` env, default to the latest released tag), and resolve the `gh:` vs mounted-`/data/service-template` source ambiguity (pick one). Record the resolved ref in `.copier-answers.yml`/the scaffold log. *Result:* two consecutive scaffolds of the same project produce the same template commit; a test asserts the ref is passed through. Depends on the §6.2 source decision (one open question to close first).

### 6.3 Adopt a service-template release

Production scaffolding uses `scheduler.service_template_source = gh:vladmesh/service-template` and the
explicit `scheduler.service_template_ref` system config. The baseline is tag `0.3.0`. The local
`/data/service-template` mount is not part of the production path.

To adopt a release, change `scheduler.service_template_ref` in `scripts/system_configs.yaml`, seed the
config, run the template contract/integration suite and a GitHub scaffold smoke, then merge the config
change. Do not point the config at `HEAD`, `main`, or another floating branch. Each scaffold records the
requested ref and Copier's resolved `_commit` in the project config under `service_template`.

3. **Introduce a shared compose-contract constant (codegen) + close the template nit.** Replace the duplicated string literals `infra/compose.base.yml` / `infra/compose.dev.yml` (`compose_runner.py:21`) with one named constant, and file a one-line template fix removing the dangling `infra/compose.frontend.yml` reference. *Result:* one place defines the compose-file contract in codegen; grep shows no stray literal; template reference no longer dangles. Independent of 1 and 2.

4. **Verify and document the cross-UID uv-cache and mark iteration 4 done (both repos).** Add a smoke assertion that a worker `make setup` reuses the shared `uv-cache` (cache hit, and no permission error across the root/uid-1000 boundary); update `plan-tooling-removal.md` iteration 4 to DONE with the codegen commits. If cross-UID sharing errors, split the caches. *Result:* a reproducible check of the cache benefit, and the plan matches reality. Depends on 3 only for sequencing convenience.
