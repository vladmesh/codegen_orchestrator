# Architecture Audit

Аудит спецификаций новой архитектуры. Дата: 2026-01-11.

**Статус:** Требует доработки перед началом реализации.

---

## Summary

| Severity | Count | Description |
|----------|-------|-------------|
| Critical | 0 | ✅ Все критические проблемы решены |
| Medium | 7 | Несогласованности в контрактах |
| Low | 12 | Недостаточно проработано |

---

## Medium Issues (Contract Inconsistencies)

### 1. CONTRACTS.md Queue Overview incomplete

**File:** `CONTRACTS.md`

**Problem:** Queue Overview table contains only 3 queues:
```
engineering:queue, deploy:results, deploy:queue
```

But document describes 15+ queues below. Table is misleading.

**Resolution:**
- [ ] Create complete Queue Registry table with ALL queues

---

### 2. Missing DTOs

**File:** `CONTRACTS.md`

DTOs mentioned but not defined:
- `AllocationDTO` (mentioned in API description)
- `TaskExecutionDTO` (mentioned in API description)
- `ScaffolderResult` (needed for error handling)
- `TesterInput/Output` (even if MVP stub, contract needed)

**Resolution:**
- [ ] Add missing DTO definitions
- [ ] Or explicitly mark as "Post-MVP" with stub contracts

---

### 3. File structure diagram outdated

**File:** `CONTRACTS.md`

End of file shows:
```
dto/
├── project.py
├── task.py
└── user.py
```

Missing files defined earlier in same document:
- `server.py`
- `incident.py`
- `service_deployment.py`
- `agent_config.py`

**Resolution:**
- [ ] Update file structure to include all DTOs

---

### 4. GitHub Client usage in API contradicts "thin API" principle

**File:** `shared/github_client.md`

Section 4.1 shows:
```python
# services/api/src/routers/projects.py
@router.post("/projects")
async def create_project(data: ProjectCreate):
    github = GitHubAppClient()
    repo = await github.provision_repo(...)
```

But `api.md` states API should be "thin CRUD layer" with NO external API calls!

**Resolution:**
- [ ] Remove GitHub example from API usage
- [ ] Clarify: GitHub calls happen in Scaffolder, not API

---

### 5. Engineering vs Developer terminology inconsistent

**Files:** Multiple

| Context | Term Used |
|---------|-----------|
| Queue | `engineering:queue` |
| Message | `EngineeringMessage` |
| Result | `EngineeringResult` |
| Worker type | `developer` |
| Subgraph | `Engineering Subgraph` |
| Node | `DeveloperNode` |

**Resolution:**
- [ ] Standardize: "Engineering" = process/flow, "Developer" = role/worker
- [ ] Add to GLOSSARY.md

---

### 6. Consumer vs Service naming confusion

**File:** `GLOSSARY.md`

States:
> "engineering-consumer — processes engineering:queue"

But there's no `engineering-consumer` service. It's part of `langgraph` service.

**Resolution:**
- [ ] Clarify: "consumer" is logical role within langgraph, not separate service
- [ ] Update GLOSSARY.md

---

### 7. WorkerLifecycleEvent states inconsistent

**Files:** `worker_wrapper.md`, `CONTRACTS.md`

| File | States |
|------|--------|
| `worker_wrapper.md` section 4.3 | `started`, `ready`, `busy`, `completed`, `failed`, `stopped` |
| `CONTRACTS.md` WorkerLifecycleEvent | `started`, `completed`, `failed` |

**Resolution:**
- [ ] Align lifecycle states across documents
- [ ] Document which states are published when

---

## Low Priority Issues (Underspecified)

### 8. Retry logic not specified

**Files:** `langgraph.md`, `TESTING_STRATEGY.md`

Mentioned:
> "Developer retries (up to N times)"

Not defined:
- How many retries? 3? 5?
- Exponential backoff?
- How is retry context passed to developer?
- How to distinguish retry-able vs fatal errors?

**Resolution:**
- [ ] Add retry policy section to `langgraph.md`
- [ ] Define retry count, backoff strategy
- [ ] Define error classification

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

### 11. Error propagation not detailed

**Files:** Multiple

Unspecified error scenarios:
- Redis unavailable during XADD - what does CLI do?
- API timeout during task creation - retry? fail?
- Worker Manager cannot spawn container - how does LangGraph know?
- Ansible playbook hanging - timeout? kill?

**Resolution:**
- [ ] Add error handling section to each service spec
- [ ] Define timeout values
- [ ] Define retry policies

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

### 13. MIGRATION_PLAN.md numbering broken

**File:** `MIGRATION_PLAN.md`

```
Phase 1: items 2, 3, 4, 5
Phase 2: items 4, 7 (item 4 duplicated!)
Phase 3: item 83 (?!), item 7 (duplicated!)
```

**Resolution:**
- [ ] Fix numbering
- [ ] Use consistent format

---

### 14. Phase dependencies not explicit

**File:** `MIGRATION_PLAN.md`

Questions:
- Does Worker Wrapper depend on CLI?
- Does Worker Manager depend on Wrapper? (yes, for base image)
- Does Scaffolder depend on GitHub Client? (yes)

**Resolution:**
- [ ] Add dependency graph or DAG
- [ ] Mark blocking dependencies

---

### 15. No acceptance criteria per service

**File:** `MIGRATION_PLAN.md`

Generic "Definition of Done" exists, but no service-specific criteria:
- For API: which endpoints must work?
- For LangGraph: which scenarios must pass?

**Resolution:**
- [ ] Add acceptance criteria checklist per service

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

### 18. Rate limiting absent

**Scope:** Overall architecture

Critical for:
- LLM API calls (expensive!)
- GitHub API (5000 req/hour limit)
- External API calls

**Resolution:**
- [ ] Add rate limiting strategy
- [ ] Define limits per resource

---

### 19. Consumer Groups and DLQ not defined

**Scope:** Redis Streams usage

Not described:
- Consumer Group naming convention
- Acknowledgement strategy (XACK)
- Dead Letter Queue for failed messages
- Max retry before DLQ

**Resolution:**
- [ ] Add "Redis Streams Patterns" section to CONTRACTS.md or new doc

---

## Testing Gaps

### 20. Docker API mocking unresolved

**File:** `TESTING_STRATEGY.md`

Open question:
> "How to mock Docker API in Worker Manager? Testcontainers?"

Critical for CI - can't run real Docker in GitHub Actions without privileged mode.

**Options:**
- Mock Docker SDK
- Testcontainers with dind
- Integration tests only in special environment

**Resolution:**
- [ ] Make decision
- [ ] Document in TESTING_STRATEGY.md

---

### 21. SSH testing unresolved

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

1. **Create complete Queue Registry** - single table with ALL queues, producers, consumers, DTOs
2. **Resolve Critical Issues** - items 1-4 must be fixed before coding
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
12. **Add rate limiting** - LLM, GitHub API

---

## Change Log

| Date | Author | Changes |
|------|--------|---------|
| 2026-01-11 | Claude | Initial audit |
