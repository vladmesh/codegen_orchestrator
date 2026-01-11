# Architecture Audit

Аудит спецификаций новой архитектуры. Дата: 2026-01-11.

**Статус:** Требует доработки перед началом реализации.

---

## Summary

| Severity | Count | Description |
|----------|-------|-------------|
| Critical | 0 | ✅ Все критические проблемы решены |
| Medium | 0 | ✅ Все несогласованности устранены |
| Low | 5 | Недостаточно проработано |

---

## Medium Issues (Contract Inconsistencies)

✅ **Все Medium issues решены.**

---

## Low Priority Issues (Underspecified)

---

### 9. Secrets handling - vault not described

**File:** `CONTRACTS.md`

`AnsibleDeployMessage` has:
```python
github_token_ref: str    # Key to fetch GitHub Token from Vault/Secrets
secrets_ref: str         # Key to fetch Project Secrets
```

Questions:
- What vault? HashiCorp? AWS? PostgreSQL table?
- Where is mapping `ref → actual_value`?
- Who resolves refs? SecretResolverNode? infra-service?
- Secret rotation strategy?

**Resolution:**
- [ ] Add `SECRETS.md` document
- [ ] Define vault choice and API
- [ ] Document resolution flow

---

### 10. Resource limits not specified

**File:** `worker_manager.md`

Pause/Resume described, but missing:
- Max concurrent workers
- CPU/Memory limits per container
- Queue if limit reached
- Docker daemon overload handling

**Resolution:**
- [ ] Add "Resource Management" section
- [ ] Define limits and queue strategy

---

---

### 12. Session management edge cases

**File:** `worker_wrapper.md`

States:
> "Session rotation: 30 min idle → new session"

Not addressed:
- User continues conversation after 31 min - context lost?
- How to handle gracefully?
- What if Claude itself reset context?

**Resolution:**
- [ ] Document session expiry UX
- [ ] Define context recovery strategy (if any)

---

---

## Architecture Gaps

### 16. Single Points of Failure not addressed

**Scope:** Overall architecture

Not mentioned:
- Redis Sentinel/Cluster for HA
- PostgreSQL replication
- Graceful degradation when dependencies down

**Resolution:**
- [ ] Add "High Availability" section to README.md
- [ ] Or explicitly mark as "Post-MVP"

---

### 17. Observability incomplete

**Files:** Multiple

Mentioned:
- `structlog` for logging
- `LangSmith` for agent tracing

Not mentioned:
- Metrics (Prometheus/StatsD?)
- Distributed tracing between services (OpenTelemetry?)
- Alerting (PagerDuty? Telegram?)
- Dashboards

**Resolution:**
- [ ] Add `OBSERVABILITY.md` document
- [ ] Or mark as "Post-MVP"

---

---

## Testing Gaps

### 19. SSH testing unresolved

**File:** `TESTING_STRATEGY.md`

Open question:
> "How to test real SSH connections in Infra Service?"

**Options:**
- Testcontainers with sshd
- Mock paramiko/asyncssh
- E2E only

**Resolution:**
- [ ] Make decision
- [ ] Document in TESTING_STRATEGY.md

---

## Recommendations

### Before Implementation

1. ~~**Create complete Queue Registry**~~ ✅ Done - single table with ALL queues, producers, consumers, DTOs
2. ~~**Add missing DTOs**~~ ✅ Done - AllocationDTO, TaskExecutionDTO added
3. **Add sequence diagrams for error flows** - not just happy path
4. **Specify retry policy** - globally and per-operation
5. **Describe secrets architecture** - vault choice, resolution, rotation
6. **Fix MIGRATION_PLAN.md** - numbering, dependencies

### During Implementation

7. **Add resource limits** to worker_manager
8. **Resolve testing open questions** before writing tests
9. **Standardize terminology** - Engineer/Developer

### Post-MVP Tracking

10. **Create OBSERVABILITY.md** - metrics, tracing, alerting
11. **Document HA strategy** - Redis cluster, PG replication
12. ~~**Add rate limiting**~~ ✅ Done - documented in CONTRACTS.md and github_client.md

---

## Change Log

| Date | Author | Changes |
|------|--------|---------|
| 2026-01-11 | Claude | Resolved: Error handling & Redis patterns (#8, #11, #18) - created ERROR_HANDLING.md |
| 2026-01-11 | Claude | Resolved: MIGRATION_PLAN issues (#13, #14, #15) - complete rewrite with dependency graph and acceptance criteria |
| 2026-01-11 | Claude | Resolved: Engineering vs Developer terminology (Issue #1) - added clarification to GLOSSARY.md |
| 2026-01-11 | Claude | Resolved: Consumer vs Service naming (Issue #2) - clarified consumer as role in GLOSSARY.md |
| 2026-01-11 | Claude | Resolved: Rate limiting (Issue #18) - added to CONTRACTS.md and github_client.md |
| 2026-01-11 | Claude | Resolved: GitHub usage in API (Issue #1) |
| 2026-01-11 | Claude | Resolved: File structure diagram (Issue #1) |
| 2026-01-11 | Claude | Resolved: Docker API mocking (Variant D) |
| 2026-01-11 | Claude | Initial audit |
