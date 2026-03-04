# Code Audit Report

**Date:** 2026-03-04
**Scope:** `services/`, `shared/`, `packages/` (256 Python files, excluding test files and `docs/`/`.claude/` directories)
**Tools:** Ruff (E, F, I, UP, B, C4 rules), manual pattern analysis

---

## Summary

| Category                        | Count | Critical | Major | Minor |
|---------------------------------|------:|----------|-------|-------|
| Dead code / unused artifacts    |     2 |        0 |     1 |     1 |
| Large files (>500 lines)        |     6 |        0 |     3 |     3 |
| Default env var values          |    44 |        3 |     5 |    36 |
| print() in production code      |    10 |        0 |     2 |     8 |
| Broad except clauses            |    23 |        0 |     6 |    17 |
| Swallowed exceptions (pass)     |    10 |        0 |     4 |     6 |
| Duplicated code across services |     2 |        0 |     2 |     0 |
| TODO/FIXME comments             |     2 |        0 |     0 |     2 |
| Subprocess without shell=False  |     4 |        0 |     4 |     0 |
| **Total**                       |**103**|    **3** | **27**| **73**|

Ruff linting (E, F, I, UP, B, C4) passes clean -- no unused imports (F401) or unused variables (F841) in production code.

---

## 1. Dead Code / Unused Artifacts

### 1.1 Standalone debug script never referenced (major)

`services/langgraph/src/list_repos.py` (72 lines) is a standalone script that searches for "palindrome" repos. It is not imported or referenced by any other file. It uses `print()` throughout and manipulates `sys.path` directly. Likely a one-off debugging artifact.

```
services/langgraph/src/list_repos.py:1-72 (entire file)
```

### 1.2 Unreachable pass after print in tool_registry (minor)

```
shared/schemas/tool_registry.py:87-88
    print(f"Warning: Failed to load CLI commands: {e}")
    pass  # redundant pass after print
```

---

## 2. Large Files (>500 lines) -- Split Candidates

| File | Lines | Severity |
|------|------:|----------|
| `services/langgraph/src/workers/engineering_worker.py` | 1088 | major |
| `shared/clients/github.py` | 986 | major |
| `services/worker-manager/src/manager.py` | 828 | major |
| `services/api/src/routers/rag.py` | 688 | minor |
| `services/langgraph/src/subgraphs/devops/nodes.py` | 644 | minor |
| `services/infra-service/src/provisioner/node.py` | 615 | minor |

**Recommendation:** The top 3 files exceed 800 lines. `engineering_worker.py` at 1088 lines is the strongest split candidate -- consider extracting scaffold logic, CI monitoring, and worker lifecycle into separate modules.

---

## 3. Security Issues

### 3.1 Default env var values violating fail-fast policy

The project rule is: "Never use default values for env vars -- fail fast with RuntimeError." The following violate this in security-sensitive contexts.

#### Critical (secrets/credentials defaulting to empty or placeholder)

| File | Line | Variable | Default |
|------|------|----------|---------|
| `shared/notifications.py` | 21 | `TELEGRAM_BOT_TOKEN` | `""` |
| `shared/notifications.py` | 22 | `API_BASE_URL` | `""` |
| `shared/clients/github.py` | 26 | `GITHUB_APP_PRIVATE_KEY_PATH` | `"/app/keys/github_app.pem"` |

The notifications module silently degrades when `TELEGRAM_BOT_TOKEN` is empty. The GitHub client assumes a default key path that may not exist, leading to confusing runtime errors instead of clear startup failures.

#### Major (identity/URL defaults masking misconfiguration)

| File | Line | Variable | Default |
|------|------|----------|---------|
| `services/infra-service/src/clients/api.py` | 20 | `API_BASE_URL` | `"http://api:8000"` |
| `packages/orchestrator-cli/src/.../engineering.py` | 21 | `ORCHESTRATOR_USER_ID` | `"unknown"` |
| `packages/orchestrator-cli/src/.../respond.py` | 32 | `ORCHESTRATOR_USER_ID` | `"unknown"` |
| `packages/orchestrator-cli/src/.../deploy.py` | 21 | `ORCHESTRATOR_USER_ID` | `"unknown"` |
| `services/langgraph/src/nodes/developer.py` | 288 | `SERVICE_TEMPLATE_REPO` | `"gh:vladmesh/service-template"` |

The `ORCHESTRATOR_USER_ID` defaulting to `"unknown"` means operations proceed with an unidentifiable actor -- audit trail is broken.

#### Minor -- acceptable (tuning constants with sensible defaults): 36 instances

Timeout values, poll intervals, and operational constants in `services/langgraph/src/config/constants.py` (19 instances), `services/infra-service/src/config/constants.py` (12 instances), and `shared/log_config/config.py` (3 instances). These are configuration knobs with reasonable defaults and are acceptable per convention.

### 3.2 Subprocess calls without shell=False verification (major)

```
services/infra-service/src/provisioner/recovery.py:73
services/infra-service/src/provisioner/ssh_manager.py:34
services/infra-service/src/provisioner/ssh_manager.py:93
services/worker-manager/src/compose_runner.py:180
```

These `subprocess` calls should be audited to confirm they do not pass untrusted input. While they likely use controlled command arrays, the Ruff S603 flag indicates they should be reviewed.

### 3.3 print() statements in production code (should use structlog)

#### Major -- logging lost in production

| File | Line | Context |
|------|------|---------|
| `shared/schemas/tool_registry.py` | 87 | `print(f"Warning: Failed to load CLI commands: {e}")` |
| `packages/worker-wrapper/src/worker_wrapper/main.py` | 25 | `print("Healthcheck passed")` |

The `tool_registry.py` print is particularly problematic: it uses `print()` to report a warning about failed CLI command loading, which will not appear in structured JSON logs in production.

The `worker_wrapper/main.py` healthcheck print runs before logger setup, which is acceptable but should ideally use `sys.stdout.write()` to signal intent.

#### Minor -- CLI output (acceptable)

48 instances of `console.print()` in `packages/orchestrator-cli/` commands. These use Rich's `console.print()` for CLI user output, which is the correct pattern for a CLI tool. Not flagged.

#### Minor -- Ansible inventory script (acceptable)

4 instances in `services/infra-service/ansible/inventory/api_inventory.py`. This is a standalone Ansible dynamic inventory script that must use `print()` for JSON output to stdout. Acceptable.

#### Minor -- Debug script

6 instances in `services/langgraph/src/list_repos.py`. Already flagged as dead code (Section 1.1).

---

## 4. Broad Exception Handling

### 4.1 Swallowed exceptions (except + pass) in production code (major)

These silently discard errors, making debugging difficult.

| File | Line | Exception Type | Severity |
|------|------|---------------|----------|
| `services/worker-manager/src/events.py` | 85 | `Exception` | major |
| `services/worker-manager/src/events.py` | 89 | `Exception` | major |
| `services/worker-manager/src/events.py` | 93 | `Exception` | major |
| `services/worker-manager/src/events.py` | 104 | `Exception` | major |
| `services/worker-manager/src/docker_ops.py` | 82 | `Exception` | minor |
| `services/worker-manager/src/main.py` | 116 | `Exception` | minor |

The `events.py` cleanup block (lines 83-95) swallows 4 separate `except Exception: pass` blocks during shutdown. Even in cleanup, a `logger.debug()` call would aid debugging.

### 4.2 Broad except clauses (not swallowed but overly broad)

23 total `except Exception:` clauses in production code. Notable cases:

| File | Line | Notes |
|------|------|-------|
| `services/worker-manager/src/routers/compose.py` | 82 | Catches all exceptions in route handler |
| `services/worker-manager/src/routers/compose.py` | 101 | Catches all exceptions in route handler |
| `services/api/src/main.py` | 45 | Startup exception handling |
| `services/api/src/routers/projects.py` | 119 | Route handler catch-all |
| `services/langgraph/src/po/consumer.py` | 186 | Message processing catch-all |
| `services/telegram_bot/src/main.py` | 326 | Bot error handler |

Most of these log the exception but could benefit from narrower exception types.

---

## 5. Code Duplication

### 5.1 Identical file: infra_client.py (major)

```
services/langgraph/src/clients/infra_client.py  (279 lines)
services/infra-service/src/clients/infra_client.py  (279 lines)
```

These files are byte-for-byte identical. This client should live in `shared/` and be imported by both services.

### 5.2 Near-identical constants (major)

```
services/langgraph/src/config/constants.py  (68 lines)
services/infra-service/src/config/constants.py  (55 lines)
```

The `Paths`, `Timeouts`, and `Provisioning` classes are duplicated between these two files with identical values. The `langgraph` version adds `CI` and worker-specific timeout classes. The shared portions (`Timeouts.SSH_COMMAND`, `Timeouts.PROVISIONING`, `Timeouts.REINSTALL`, `Timeouts.PASSWORD_RESET`, `Timeouts.ACCESS_PHASE`, `Timeouts.SERVICE_DEPLOY`, all of `Provisioning`, and `Paths.SSH_KEY`) should be extracted to `shared/`.

---

## 6. TODO/FIXME Comments

Only 2 TODO comments found in production code (excluding test data strings):

| File | Line | Comment | Severity |
|------|------|---------|----------|
| `shared/notifications.py` | 143 | `# TODO: Add is_admin field filtering when implemented` | minor |
| `services/api/src/routers/servers.py` | 282 | `# TODO: Trigger LangGraph provisioner node via queue/webhook` | minor |

Both indicate planned but not yet implemented features. Low risk.

---

## 7. Ruff Lint Status

The project passes all configured Ruff rules (E, F, I, UP, B, C4) clean. No violations in production or test code.

Rules **not currently enabled** that would catch issues found in this audit:
- `S` (flake8-bandit): Would catch S603 (subprocess calls), S110 (try-except-pass). 4+6 issues.
- `PLR` (Pylint refactoring): Would flag overly complex functions in large files.
- `C901` (McCabe complexity): Would flag functions in the 6 large files.
- `BLE` (blind-except): Would catch the 23 broad `except Exception:` clauses.

**Recommendation:** Consider adding `S110`, `BLE001` to Ruff's `select` list for incremental improvement.

---

## 8. Recommended Actions (Priority Order)

### High Priority
1. **Extract `infra_client.py` to `shared/`** -- eliminates 279 lines of duplicated code that will inevitably drift.
2. **Extract shared constants** (`Timeouts`, `Provisioning`, `Paths.SSH_KEY`) to `shared/config/constants.py`.
3. **Fix critical getenv defaults** -- `TELEGRAM_BOT_TOKEN` and `API_BASE_URL` in `shared/notifications.py` should fail fast or clearly document silent degradation.

### Medium Priority
4. **Split `engineering_worker.py`** (1088 lines) -- extract scaffold, CI gate, and worker lifecycle into separate modules.
5. **Replace `print()` with structlog** in `shared/schemas/tool_registry.py:87`.
6. **Add logging to swallowed exceptions** in `services/worker-manager/src/events.py` cleanup blocks.
7. **Fix `ORCHESTRATOR_USER_ID` defaults** -- either require it or use a sentinel that downstream code can detect.

### Low Priority
8. **Delete `services/langgraph/src/list_repos.py`** -- dead debug script.
9. **Enable Ruff S110 and BLE001 rules** to catch future occurrences.
10. **Split `shared/clients/github.py`** (986 lines) -- separate app auth, repo operations, and workflow management.
