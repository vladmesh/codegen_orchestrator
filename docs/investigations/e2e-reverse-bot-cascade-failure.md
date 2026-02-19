# Post-Mortem: E2E test `reverse-bot` — Cascade Failure

**Date**: 2026-02-17
**Project**: `reverse-bot` (ID: `9e07b9df`)
**Branch**: `feat/deploy-architecture`
**Severity**: Critical — pipeline reported success to user despite no code being committed

## Summary

Ordered a project via Telegram to E2E-test the deploy pipeline (iteration 7: self-hosted registry).
The developer worker (Claude Code in a container) ran successfully (exit code 0) but **did not commit any code**.
Despite `commit_sha=None`, the entire pipeline continued: tester "passed", CI was "skipped", PO told the user "Code is ready! Starting deploy", and deploy was triggered — which obviously failed.

**Root cause**: No validation of `commit_sha` at any point in the pipeline. Five independent safety checks were either missing or fail-open.

## Chronology

| Time (UTC) | Event | Status |
|------------|-------|--------|
| 22:06:24 | PO accepted request, created project `9e07b9df` (backend + tg_bot) | OK |
| 22:06:31 | Engineering task created, scaffolding triggered | OK |
| 22:06:40 | Scaffolding complete, repo `project-factory-organization/reverse-bot` pushed | OK |
| 22:06:42 | Developer node: waited for scaffold, requested worker spawn | OK |
| 22:07:06 | Worker container created, Claude Code started | OK |
| **22:12:10** | **Developer node: `success=True`, `commit_sha=None`** | **BUG 1** |
| 22:12:10 | Tester node: hardcoded `passed=True` (stub) | BUG 2 |
| 22:12:10 | `ci_check_skip_no_repo_url` — CI check skipped, returned True | **BUG 3** |
| 22:12:10 | Engineering task marked "completed", PO notified user "Code ready!" | **BUG 4** |
| 22:12:12 | Deploy auto-triggered without validating commit_sha | **BUG 5** |
| 22:12:22 | 9 GitHub secrets written (DOTENV, DEPLOY_*, REGISTRY_*) | OK |
| 22:12:24 | `deploy.yml` workflow dispatched on GitHub Actions | OK |
| 22:13:12 | Deploy workflow **failed** (nothing to deploy) | Expected |

## Five Points of Failure

### BUG 1: Worker Wrapper does not extract `commit_sha`

**File**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py:192-200`

Claude Code ran with `--output-format json` and returned a JSON response with a `result` text field, but **without** `<result>` structured tags containing `commit_sha`.

The wrapper's parsing logic:
1. Looked for `<result>` tags → not found
2. Fell back to `ResultParser.extract_text()` → returned `{"content": "...", "status": "success"}`
3. This dict has **no `commit_sha` key**

Then `worker_spawner.py:218` does `output_resp.get("commit_sha")` → **None**.

**The wrapper never checks `git log` independently** — it relies entirely on the agent outputting structured data.

### BUG 2: Developer Node accepts `commit_sha=None` as success

**File**: `services/langgraph/src/nodes/developer.py:127-144`

```python
if worker_result.success:  # ← True (exit code 0)
    return {
        "engineering_status": "done",
        "commit_sha": worker_result.commit_sha,  # ← None — no validation!
    }
```

Only the `success` boolean is checked. For `action=create` there **must** be a commit. No assertion.

### BUG 3: CI Gate is fail-open

**File**: `services/langgraph/src/workers/engineering_worker.py:121-124`

```python
repo_url = project.get("repository_url", "")
if not repo_url or "github.com/" not in repo_url:
    logger.warning("ci_check_skip_no_repo_url", task_id=task_id)
    return True  # ← SUCCESS! Should be False
```

This is a **fail-open** gate. When `repository_url` is missing, CI is considered "passed".

Why `repository_url` was missing: the `project` dict was fetched **before** the scaffolder ran. By the time CI check happens, scaffolder has updated the project in the DB, but the engineering worker is still using the stale in-memory dict. The developer node fetches a fresh copy internally (and uses repo_url successfully), but the outer `project` variable is never refreshed.

### BUG 4: No commit_sha validation before deploy trigger

**File**: `services/langgraph/src/workers/engineering_worker.py:657-689`

After CI "passes" (actually skipped), deploy is queued immediately:

```python
if not skip_deploy:
    # ← NO CHECK: result.get("commit_sha") could be None
    await redis.redis.xadd(DEPLOY_QUEUE, {...})
```

### BUG 5: User notified "success" before deploy validates

**File**: `services/langgraph/src/workers/engineering_worker.py:647-655`

```python
await publish_callback_event(
    redis, callback_stream, "completed", task_id,
    "Engineering task completed, CI passed",
    ...
)
```

This sends a `system_event` to the PO, which then tells the user "Code ready! Starting deploy" — before deploy has even started, let alone validated that there's anything to deploy.

## Additional Findings

### Worker container corrupted its own environment

After Claude Code ran, the worker container became **unhealthy**:

```
ModuleNotFoundError: No module named 'shared.log_config'
```

Claude Code (running with `--dangerously-skip-permissions`) likely modified system packages inside the container. The healthcheck command (`worker-wrapper health`) imports `shared.log_config` which was destroyed.

This is a separate isolation issue — the agent should not be able to modify system-level packages.

### Provisioner timeout (unrelated)

Langgraph logs show `provisioner_proxy_timeout` for vps-267179 and vps-267180 (1200 sec timeout). This is a pre-existing issue with the infra-service provisioning queue — not related to this test.

## Recommended Fixes

### Level 1 — Fail-fast (start here)

**1. Developer Node: validate `commit_sha` for create/feature actions**

`services/langgraph/src/nodes/developer.py:127`

```python
if worker_result.success:
    if not worker_result.commit_sha:
        return {
            "engineering_status": "blocked",
            "errors": ["Worker reported success but no commit was made"],
        }
    # ... existing success path
```

**2. CI Gate: fail-closed instead of fail-open**

`services/langgraph/src/workers/engineering_worker.py:122-124`

```python
if not repo_url or "github.com/" not in repo_url:
    logger.error("ci_check_fail_no_repo_url", task_id=task_id)
    return False  # Was: return True
```

**3. Deploy gate: check commit_sha before queuing deploy**

`services/langgraph/src/workers/engineering_worker.py:657` (before `if not skip_deploy`)

```python
if not result.get("commit_sha"):
    logger.error("deploy_blocked_no_commit", task_id=task_id)
    await publish_callback_event(
        redis, callback_stream, "failed", task_id,
        "Development completed but no code was committed",
        user_id=user_id, project_id=project_id,
    )
    return {"status": "failed", "error": "No commit_sha"}
```

### Level 2 — Robustness

**4. Worker Wrapper: extract commit_sha from git independently**

After Claude Code finishes, run `git log -1 --format=%H` in the workspace and include `commit_sha` in the result dict. Don't rely on the agent producing `<result>` tags.

**5. Refresh project dict before CI check**

Before calling `_wait_for_ci_and_fix()`, re-fetch the project from API to get the latest `repository_url`.

**6. Decouple user notification from deploy trigger**

Send "completed" to user only after deploy succeeds, not after engineering "completes". Or at minimum, send an honest status: "Code written, waiting for CI and deploy".

### Level 3 — Quality (later)

**7. Replace tester stub** with at least `make lint` inside the worker container.

**8. Worker sandbox hardening**: mount site-packages as read-only to prevent Claude Code from corrupting the wrapper's own dependencies.

## Files Referenced

| File | Lines | Issue |
|------|-------|-------|
| `packages/worker-wrapper/src/worker_wrapper/wrapper.py` | 192-200 | No commit_sha extraction |
| `packages/worker-wrapper/src/worker_wrapper/result_parser.py` | 22-34 | Only parses `<result>` tags |
| `services/langgraph/src/clients/worker_spawner.py` | 207-218 | Passes None commit_sha through |
| `services/langgraph/src/nodes/developer.py` | 127-144 | No commit_sha validation |
| `services/langgraph/src/subgraphs/engineering.py` | 105-135 | Tester stub always passes |
| `services/langgraph/src/workers/engineering_worker.py` | 121-124 | CI gate fail-open |
| `services/langgraph/src/workers/engineering_worker.py` | 484-501 | No commit_sha check before deploy path |
| `services/langgraph/src/workers/engineering_worker.py` | 647-689 | Premature success notification + deploy trigger |
