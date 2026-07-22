# Stage 7 live-server preflight

- Date: 2026-07-14
- Initial orchestrator commit: `4d364234bf7037efa55698355d1bcba270650960`
- Retest commit: `a9b98f51` (PR #62 merged)
- Target: `vps-273978` / `5vei.l.time4vps.cloud` / `185.81.166.84`
- Existing workload: `personal_site` at `https://vladmesh.dev`
- Scope: preparation and read-only/safe checks only. No live project, provisioning or reinstall was started.

## Result

**LIVE RUN REACHED ENGINEERING; BLOCKED BY NOOP/BRANCH-PROTECTION CONTRACT.** The target VPS,
backup, SSH identity, server record, port fence, GitHub App authentication and external registry
passed preflight. PR #62 fixed the first-run scaffold and ownership-cleanup defects. The latest run
completed scaffold and created a real worker, but the deterministic noop command pushed directly to
protected `main` instead of the story branch. GitHub rejected the push and engineering failed before
deploy.

## Completed preparation

- Fast-forwarded the local checkout to `origin/main`, including PR #60 (fail-closed live harness)
  and PR #61 (per-server SSH user).
- Generated a valid `SECRETS_ENCRYPTION_KEY` in the gitignored project `.env`. This was safe because
  the persisted database contained zero servers, projects, API keys and agent configs before the
  key was created.
- Created a verified backup at
  `/home/dev/backups/codegen-stage7-20260714T135450Z`:
  - PostgreSQL custom-format dump, readable by `pg_restore`, 65 TOC entries;
  - `/opt/services/personal_site` deployment config archive, including `.env`, Compose and Caddy;
  - Docker inspect data, Compose status, host resources and checksums;
  - all recorded checksums pass.
- Recorded healthy target baseline:
  - 1 vCPU, 1967 MiB RAM, 2 GiB swap;
  - about 1.38 GiB memory available at preflight time;
  - 25 GiB filesystem, about 16 GiB free;
  - four `personal_site` containers running; backend, frontend and PostgreSQL healthy;
  - `https://vladmesh.dev/` responds with the expected redirect.
- Verified SSH from a LangGraph runtime image using the exact target identity:
  `dev@185.81.166.84` with the `personal_site_deploy` key. The user can list the four site
  containers and has Docker access.
- Added the target to the orchestrator database without publishing a provisioning command:
  - handle `vps-273978`;
  - status `ready`, managed;
  - `ssh_user=dev`;
  - SSH private key encrypted at rest and retrievable through the protected key endpoint;
  - provider label `273978`, plus `adopted_existing`, `stage7_target` and
    `production_workload=personal_site` labels;
  - capacity corrected to 1 CPU, 1967 MiB RAM and 25600 MiB disk.
- Reserved the first allocator port operationally with a Docker firewall fence:
  - TCP 8000 is accepted from orchestrator IP `109.235.67.14`;
  - other forwarded traffic to TCP 8000 is rejected in `DOCKER-USER`;
  - ports 80/443 and the existing site were not changed.
- Confirmed outbound GitHub API access from the target (`HTTP 200`).
- Re-ran the offline harness/scaffolder regression suite: `29 passed`.

## Findings and blockers

### GitHub App credential resolved after initial preflight

The initial preflight found placeholder App metadata and an empty directory at the PEM mount source.
The follow-up installed the private key for App `2528501` (`project-factory-keeper-v1`) at
`secrets/github_app.pem` with mode 0600, configured organization `project-factory-organization` and
installation `100979986`, and set the container key path to `/app/keys/github_app.pem`. A real
`GitHubAppClient.get_org_token()` probe from a Compose API container succeeded. The downloaded
source copy was removed after installation.

### External registry resolved after initial preflight

The registry is available through Caddy at `https://5uoc.l.time4vps.cloud/v2/` with an automatically
issued public TLS certificate and generated basic-auth credentials. The registry, Caddy, API,
scaffolder and infra service were recreated with the final configuration. An unauthenticated probe
returns 401 and an authenticated probe returns 200. A smoke image was pushed from the orchestrator,
pulled and removed on `5vei`, then deleted from the registry. All four `personal_site` containers
remained healthy and `https://vladmesh.dev/` kept its expected 302 response.

### Project Time4VPS values are placeholders

The codegen project `.env` contains placeholder Time4VPS login/password values. Real credentials
exist in the control-panel environment, but were deliberately not copied because this adopted-server
run must not invoke provisioning. They are not required for the first noop deploy gate.

### API create ignores disk capacity

`POST /api/servers/` accepted `capacity_disk_mb=25600` but persisted the schema default `10240`.
The preflight corrected the live record with the supported PATCH endpoint. This does not block the
run, but it is a reproducible API bug worth fixing separately.

## Safety state after preflight

- No test project, GitHub repository, Redis job, Application, port-allocation row or deployment was
  created.
- No provisioning or reinstall queue entry was published.
- `personal_site` remained healthy after backup, server registration and firewall changes.
- The temporary API/DB/Redis startup used only to migrate and write the server record. The core
  stack was not declared live-ready with placeholder GitHub credentials.

## Required next steps

1. Make the noop engineering command push its checked-out story branch, matching the mega-test
   contract, and publish a structured failure result even when git push fails.
2. Fix Redis blocking-read timeout handling. A temporary `socket_timeout=60` override keeps
   LangGraph alive, but the shared consumers still log a timeout every six seconds.
3. Add an internal registry route to the production Compose configuration. Cleanup currently needs
   a temporary Caddy network alias for `5uoc.l.time4vps.cloud` to avoid host hairpin timeouts.
4. Make live cleanup remove owned worker containers and make `test-live-clean` verify manifests and
   current-schema allocations before reporting success.
5. Rebuild, rerun `make test-live-mega`, then compare `personal_site` with this baseline again.

## First live-run attempt

- Started: 2026-07-14 19:10 EEST.
- Run/project ID: `bb99e7db-3578-41c2-b45c-3429becbf4ff`.
- Repository: `project-factory-organization/live-test-e52efe7c`.
- Command: `make test-live-mega`.
- Result: exit 2 after 130.99 seconds; scaffold status remained `draft`.
- Primary failure: the scaffolder successfully created the repository, installed registry secrets,
  rendered the service-template project and pushed it, then `_capture_tree()` raised
  `FileNotFoundError: tree` because the runtime image lacks that executable.
- Cleanup failure 1: the cleanup subprocess looked for the GitHub App PEM at
  `/app/secrets/github_app_key.pem` instead of the configured `/app/keys/github_app.pem`.
- Cleanup failure 2: `_cleanup_db()` attempted to delete `port_allocations.project_id`, but that
  column does not exist in the current schema.
- Cleanup gap: GitHub Actions pushed
  `project-factory-organization/live-test-e52efe7c-backend:sha-27c9fca`; registry artifacts are not
  represented in the ownership manifest.
- Manual fail-closed recovery completed: the GitHub repository returns 404; project and repository
  DB rows are zero; port allocations are zero; the Redis entry and live manifest are absent; the
  registry manifest was deleted and garbage collection removed its blobs.
- Target safety check after recovery: all four `personal_site` containers are unchanged and healthy,
  `https://vladmesh.dev/` still returns 302, target disk usage remains 33% with 16 GiB free.
- Pytest debug dump: `docs/e2e_results/debug-full-scaffold-20260714-161219.md`.
- Raw run log: `.live-runs/mega-20260714T191015.log`.

## Retest attempts after PR #62

### Runtime refresh attempt

- Scaffold passed after the tree fallback was merged.
- Engineering still used the stale container key path because only API, scaffolder and LangGraph
  had been recreated. Engineering, deploy, architect and scheduler were then recreated and verified
  with `/app/keys/github_app.pem`.
- Cleanup completed manually and left no project, allocation, manifest, registry tag or GitHub repo.

### Worker-image attempt

- Scaffold passed and engineering allocated target port 8000.
- Worker Manager could not start `worker-base-claude:latest`; the image had been removed during
  host Docker cleanup. The run was stopped, both base images were rebuilt from commit `a9b98f51`,
  and all owned resources were removed.
- This attempt exposed two operational cleanup dependencies: LangGraph exited on the Redis socket
  timeout, and registry cleanup could not reach the public registry hostname from its container.
  Temporary Compose overrides set a 60-second Redis timeout and gave Caddy the internal alias
  `5uoc.l.time4vps.cloud`.

### Latest mega attempt

- Started: 2026-07-14 22:20 EEST.
- Run/project ID: `9210bb60-a2f3-48ba-9d94-be1784ec36c4`.
- Repository: `project-factory-organization/live-test-8e9881d3`.
- Raw log: `.live-runs/mega-20260714-222009-retry3.log`.
- Scaffold completed, captured 91 tree lines, set branch protection and enabled auto-merge.
- Engineering allocated port 8000, built capability image `worker:449e961888c0`, checked out
  `story/story-f47a0b8e`, and started the noop worker.
- The noop command ran `git push origin main`. Branch protection requires a PR, so the chained
  command stopped before emitting `<result>`. Worker Wrapper reported `Agent exited without
  reporting result`; the task became `failed` after 89.63 seconds.
- Automatic ownership cleanup removed the GitHub repository, DB project and allocation, registry
  manifest and Redis entries. The leftover owned worker container was removed separately.
- Target safety check passed: `https://vladmesh.dev/` still returns 302 and no test deployment
  reached the target server.

## Post-PR #64 mega attempt

- Started: 2026-07-15 12:00 EEST.
- Commit: `8b2b6ec3`.
- Run/project ID: `d816421c-7170-4667-bad8-69dbee9f82b8`.
- Repository: `project-factory-organization/live-test-801a8aa1`.
- Raw log: `.live-runs/mega-20260715-120017-post64.log`.
- Scaffold passed and engineering completed through a real noop worker on
  `story/story-6014eed7`; the task reached `done` and PR #1 was created with auto-merge enabled.
- Generated-project CI failed in `Run integration tests`: the backend container raised
  `PermissionError: [Errno 13] Permission denied` while writing
  `/workspace/services/backend/src/generated/registry.py`.
- Scheduler detected the failure and created deterministic fix tasks. Noop retries produced empty
  commits, so the same CI failure repeated and deploy timed out. Pytest result: one failure after
  534.90 seconds; `final_app_status` remained unset.
- Automatic cleanup proved absence: project and allocation rows are zero, worker containers and
  ownership manifests are zero, the GitHub repository returns 404, and the registry repository has
  no tags.
- Target safety check passed: all four `personal_site` containers remain up, the backend, frontend
  and PostgreSQL are healthy, and `https://vladmesh.dev/` returns 302.
