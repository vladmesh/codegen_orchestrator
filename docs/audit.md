# Code Audit

> **Date**: 2026-03-10
> **Scope**: full

## Summary
- 🔴 Critical: 0
- 🟡 Warning: 10
- 🔵 Info: 8
- **Total**: 18 issues

| Category | 🔴 | 🟡 | 🔵 |
|----------|----|----|---|
| CI Health | 0 | 1 | 0 |
| Directory structure | 0 | 3 | 3 |
| Glossary alignment | 0 | 1 | 0 |
| Docker-Compose alignment | 0 | 0 | 2 |
| Dead code | 0 | 0 | 0 |
| Code smells | 0 | 0 | 3 |
| Security | 0 | 0 | 0 |
| Test gaps | 0 | 5 | 0 |
| Dependency freshness | 0 | 0 | 0 |

---

## CI Health

🟡 `gh run list` timed out — `gh` CLI may not be authenticated on this machine. Unable to check latest CI status.

**Action:** Configure `gh auth login` or check CI manually.

---

## Directory Structure & Glossary Alignment

| Sev | Location | Issue | Glossary/Arch term | Action |
|-----|----------|-------|--------------------|--------|
| 🟡 | `services/` | Naming inconsistency: `telegram_bot` (snake_case) vs `worker-manager`, `infra-service` (kebab-case) | — | rename `telegram_bot` → `telegram-bot` for consistency |
| 🟡 | `services/api/src/utils/` | Vague `utils/` directory — only contains `webhook_security.py` | — | move `webhook_security.py` to `services/api/src/security.py` or `services/api/src/webhooks/` |
| 🟡 | `services/api/src/routers/` | 20 .py files flat in one directory, no sub-grouping | — | consider grouping routers by domain (e.g. `infra/`, `projects/`, `auth/`) |
| 🔵 | `services/api/src/schemas/` | 15 .py files flat | — | consider grouping if schemas grow further |
| 🔵 | `services/worker-manager/src/` | 12 .py files flat at source root level | — | acceptable for now, monitor growth |
| 🔵 | `shared/models/` | 17 .py files flat | — | acceptable — one model per file is a clear convention |
| 🟡 | `docs/ARCHITECTURE.md` | Missing — Glossary references architecture but no `ARCHITECTURE.md` exists | ARCHITECTURE.md | create `docs/ARCHITECTURE.md` documenting the intended system layout |

---

## Docker-Compose Alignment

| Sev | Service/Dir | Issue | Action |
|-----|-------------|-------|--------|
| 🔵 | `architect`, `deploy-worker`, `engineering-worker` | Compose-only services (workers) with no dir in `services/` | ignore — these are LangGraph subworkflows/worker profiles, not standalone services |
| 🔵 | `db`, `redis`, `caddy`, `registry` | Infrastructure services in compose, no dir in `services/` | ignore — third-party infra, no custom code needed |

No orphaned `services/` directories found — all 7 have corresponding compose services. ✅

---

## Dead Code

`make lint` — **all checks passed**, no unused imports. ✅

No dead files or zero-caller functions detected during scan.

---

## Code Smells

| Sev | File | Issue | Action |
|-----|------|-------|--------|
| 🔵 | `services/api/src/routers/servers.py` | 393 LOC — approaching 400 LOC threshold | monitor, refactor if grows |
| 🔵 | `services/scheduler/src/tasks/github_sync.py` | 382 LOC | monitor |
| 🔵 | `services/telegram_bot/src/handlers.py` | 382 LOC | monitor |

No `# noqa` comments found to review. ✅

---

## Security

No hardcoded secrets, tokens or passwords found. ✅

`subprocess.run` usage in production code:
- `services/worker-manager/src/compose_runner.py:235` — uses subprocess for docker compose operations (expected, validated input)
- `services/infra-service/src/provisioner/ssh_manager.py:34` — uses subprocess for SSH key operations (expected)

Both are appropriate for their context. ✅

---

## Test Gaps

| Sev | Service | Files without tests | Action |
|-----|---------|--------------------:|--------|
| 🟡 | `api` | 26 source files, 0 have direct unit tests | critical — all routers/schemas untested |
| 🟡 | `infra-service` | 9 source files without tests | needs at least provisioner tests |
| 🟡 | `langgraph` | 24 source files without tests | many nodes/tools/consumers untested |
| 🟡 | `scaffolder` | 3 source files without tests | small service, but zero coverage |
| 🟡 | `telegram_bot` | 6 source files without tests | handlers/keyboards/middleware untested |

Skipped tests:
- `tests/e2e/test_engineering_flow.py:84` — `@pytest.mark.skip(reason="Full flow test - enable when all services ready")` — intentional, OK

---

## Dependency Freshness

All services use `pyproject.toml` with version constraints. Lock files present for all services. ✅

No unpinned dependencies found in `pyproject.toml` dependency lists.
