# E2E Report: todo_api — Engineering OK, deploy SCP timeout

> **Date**: 2026-03-02
> **Project**: todo_api (project_id: `9e9ed21d-d84c-4d20-b349-e562ec31082f`)
> **Task**: eng-c6d243dc99cc / deploy-c6d243dc99cc
> **Test level**: C
> **Status**: Failed (deploy)
> **Worker audit**: [todo_api-20260302-levelC-4-worker.md](./todo_api-20260302-levelC-4-worker.md)

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 13:46 | Pre-flight: cleaned stale empty repo `todo-api`, servers clean |
| 13:47:28 | Project created, engineering task queued |
| 13:47:29 | Engineering task started |
| 13:47:30 | Initial commit |
| 13:47:34 | Worker container `worker-dev-todo-api-6456a16b` created |
| 13:47:35 | Scaffold phase start (copier, modules: backend) |
| 13:47:52 | Scaffold commit: `feat: scaffold todo-api with modules: backend` |
| 13:47:53 | Scaffold complete + verified (copier-answers + github-workflows) |
| 13:47:54 | Claude agent started in worker container |
| 13:53:14 | Implementation commit: `feat: implement TODO CRUD API endpoints` |
| 13:53:20 | CI run #22579039042 started (implementation) → **success** |
| 13:55:25 | Engineering task completed, deploy task created |
| 13:55:32–39 | Deploy worker sets 9 GitHub secrets (DEPLOY_HOST, DEPLOY_USER, SSH_KEY, etc.) |
| 13:55:41 | Deploy workflow dispatched |
| 13:55:56 | SCP action starts — tar files, connects to server |
| 13:56:56 | **SCP timeout**: `dial tcp ***:22: i/o timeout` |
| 13:57:01 | Deploy task marked failed |

**Engineering duration**: ~6 min (scaffold to CI pass)
**Total duration**: ~10 min (including failed deploy)

## Results

- **Engineering**: PASS — code pushed, CI green, 1 implementation commit
- **CI**: PASS — ci.yml conclusion=success (run #22579039042)
- **Deploy**: FAIL — SCP timeout connecting to server

## Commits

| SHA | Message |
|-----|---------|
| `d6471f1e` | Initial commit |
| `af4a1110` | feat: scaffold todo-api with modules: backend |
| `9ebde3e5` | feat: implement TODO CRUD API endpoints |

## CI Runs

| Run | Trigger | Conclusion |
|-----|---------|------------|
| #22578836131 | scaffold push | success |
| #22579039042 | implementation push | success |
| #22579127781 | deploy.yml dispatch | **failure** (SCP timeout) |

## Investigation

### Code path verification

Added instrumentation logging to `DeployerNode` and triggered a test deploy through the
actual code path. Confirmed the deployer produces correct values:

```
deploy_secrets_preview  server_ip=176.223.131.124  port=8000  project_name=todo_api
                        owner=project-factory-organization  repo=deploy-test  dotenv_len=0
```

All 9 GitHub secrets set successfully (204/201 responses). `DEPLOY_HOST` value is correct —
GitHub Actions masks it in logs (`dial tcp ***:22`), which proves the secret IS present and
matches the IP.

### Connectivity tests from GitHub Actions

Created `deploy-test` repo with test workflows. Results:

| Test | Target | Result |
|------|--------|--------|
| Raw SSH | vps-267179 (176.223.131.124) | **OK** |
| `appleboy/scp-action@v0.1.7` | vps-267179 | **OK** |
| `appleboy/ssh-action@v1.0.0` | vps-267179 | **OK** |
| Raw SSH | vps-267180 (80.209.235.229) | **OK** |
| `appleboy/scp-action@v0.1.7` | vps-267180 | **OK** |
| `appleboy/ssh-action@v1.0.0` | vps-267180 | **OK** |

Both servers fully accessible from GH Actions ~40 minutes after the failed deploy.
Run #22580475373 completed in ~45 seconds — no issues.

### Server-side verification

Checked SSH auth logs on vps-267179 during the 13:55–13:57 failure window: **no inbound
connection attempt from GitHub Actions IPs**. The TCP SYN packets never reached the server.
UFW allows port 22 from anywhere. sshd listening on 0.0.0.0:22. Firewall rules identical
on both servers.

### Root cause: transient GH Actions → Time4VPS routing failure

**Geography**:
- GitHub Actions runners: Azure datacenters, primarily US (IP ranges `4.148.0.0/16` etc.)
- vps-267179: Time4VPS, **Poland** (Wroclaw), AS212531
- vps-267180: Time4VPS, **Lithuania** (Vilnius), AS212531
- Orchestrator: Time4VPS, Lithuania (Vilnius), AS212531 — same network

The route from GH Actions to Time4VPS: US Azure DC → transatlantic → Europe → AS212531 → VPS.
Each GH Actions run gets a random runner in a random Azure DC, with different routing paths.
When the path has packet loss or congestion at a peering point, TCP SYN doesn't reach the
server, resulting in `dial tcp: i/o timeout`.

**Pattern across 4 runs today** (2 out of 4 had GH Actions → Time4VPS connectivity failures):

| Run | Time | Server | SCP | Issue |
|-----|------|--------|-----|-------|
| 1 | 02:00 | vps-267179 Poland | OK | backend crash (template bug) |
| 2 | 10:00 | vps-267180 Lithuania | OK | backend crash (template bug) |
| 3 | 12:50 | — | — | Docker registry login fail (GH Actions → Time4VPS registry) |
| 4 | 13:55 | vps-267179 Poland | **TIMEOUT** | this run |

### Missing: deploy retry logic

The CI pipeline has retry logic at two levels:
- **Workflow level**: `ci.yml.jinja` has 3-attempt Docker login with backoff (lines 119–135)
- **Orchestrator level**: `engineering_worker.py` has `rerun_failed_jobs()` for CI infra failures

The deploy pipeline has **neither**:
- **`deploy.yml.jinja`**: SCP and SSH actions called directly, no retry wrapper
- **`DeployerNode`**: on `RuntimeError` (workflow failed) immediately returns failure, no `rerun_failed_jobs`

This is the actionable gap — the deploy path was not covered when retry logic was added in
commit `6b74da0`.

## Problems Found

### Problem 1: Deploy has no retry logic — SCP/SSH failures are not retried

- **Type**: orchestrator + template
- **Severity**: critical (blocks Level C, 50% failure rate on today's runs)
- **Description**: When `deploy.yml` fails (SCP timeout, SSH timeout), neither the workflow
  nor the orchestrator retries. The `DeployerNode` catches the `RuntimeError` and immediately
  returns `{"status": "failed"}`. The infrastructure for retrying (`rerun_failed_jobs()` +
  `wait_for_run_completion()`) exists in `GitHubAppClient` but is only used in the engineering
  CI path, not the deploy path.
- **Root cause**: Retry logic from commit `6b74da0` only covered the CI pipeline
  (`ci.yml.jinja` + `engineering_worker.py`). The deploy pipeline (`deploy.yml.jinja` +
  `DeployerNode` in `nodes.py`) was not updated.
- **Suggested fix** (two levels):
  1. **`deploy.yml.jinja`**: Replace bare `appleboy/scp-action` with a retry wrapper
     (3 attempts with backoff), same pattern as Docker login retry in `ci.yml.jinja`
  2. **`DeployerNode`** (`nodes.py:487`): On `RuntimeError`, call
     `github.rerun_failed_jobs(run_id)` + `github.wait_for_run_completion(run_id)` before
     giving up — same pattern as `_try_infra_rerun()` in `engineering_worker.py`

### Problem 2: GH Actions → Time4VPS routing is unreliable

- **Type**: other (infrastructure)
- **Severity**: major
- **Description**: GitHub Actions runners (Azure US) have intermittent connectivity issues
  reaching Time4VPS servers (AS212531, Lithuania/Poland). 2 out of 4 Level C runs today had
  GH Actions → Time4VPS network failures (1x Docker registry login, 1x SCP timeout). The
  route crosses the Atlantic and multiple peering points, and is subject to the random runner
  assignment.
- **Mitigation options** (increasing effectiveness):
  1. Retry logic (Problem 1 fix) — handles transient failures automatically
  2. Self-hosted GH Actions runner on one of the VPS — zero-latency deploy, same AS
  3. Deploy directly from orchestrator (already in AS212531) instead of via GH Actions —
     eliminates the transatlantic dependency entirely

### Problem 3: Port allocation missing server_ip field

- **Type**: orchestrator
- **Severity**: minor
- **Description**: The port allocations API (`/api/servers/vps-267179/ports`) returns objects
  without a `server_ip` field — only `server_handle`, `port`, `service_name`, `project_id`,
  `id`. The skill instructions suggest querying port allocations for `server_ip`, but the
  field doesn't exist in the response.
- **Root cause**: The API schema for port allocations doesn't include `server_ip`. The deploy
  worker resolves the IP internally via `get_server_ip(server)` from the server record.
- **Suggested fix**: Either add `server_ip` to the port allocation API response, or update
  the E2E skill instructions to use `/api/servers/{handle}` to get the public_ip separately.
