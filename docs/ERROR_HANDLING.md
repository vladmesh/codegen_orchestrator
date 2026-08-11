# Error Handling Strategy

The common error handling strategy for all codegen_orchestrator services.

---

## 1. Error Categories

All errors are classified into 4 categories in order to decide about a retry.

| Category | Description | Examples | Retry Strategy |
|----------|-------------|----------|----------------|
| **TRANSIENT** | A temporary failure, success is likely on a retry | Redis timeout, API HTTP 503, Network glitch | **Retry** (Short backoff) |
| **UPSTREAM** | A failure of an external system that requires waiting | GitHub Rate Limit, OpenAI API Overload | **Retry** (Long backoff) |
| **PERMANENT** | A logic error, a retry is pointless | HTTP 400 Bad Request, Validation Error, 404 | **No Retry** (Fail fast) |
| **FATAL** | A critical configuration problem | Auth failed, DB Connection failed (persistent), Config missing | **Abort** (Alert admin) |

---

## 2. Retry Policy

The global retry policies. Specific values can be overridden in a service's config.

### Transient Policy
- **Count:** 3 retries
- **Backoff:** Exponential (1s, 2s, 4s)
- **Jitter:** ±100ms
- **On Exhaust:** Move to DLQ or Fail Task

### Upstream Policy
- **Count:** 5 retries
- **Backoff:** Exponential (5s, 10s, 20s, 60s...)
- **On Exhaust:** Notify Admin, Fail Task

---

## 3. Timeout Policy

Timeouts to prevent the system from hanging.

| Operation | Default Timeout | Action on Timeout |
|-----------|-----------------|-------------------|
| **API Request** (Internal) | 10s | Retry (Transient) |
| **Redis Command** (XADD/XREAD) | 5s | Retry (Transient) |
| **Worker Container Spawn** | 60s | Fail (Permanent) |
| **GitHub Workflow** (deploy.yml) | 10 min | Fail Task (DeployerNode `wait_for_workflow_completion` timeout) |
| **Ansible Provisioning** | 15 min | Kill Process, Fail Task |
| **Developer Worker Task** | 30 min | Kill Container, Fail Task (or Retry if supported) |

---

## 4. Propagation Flow

How errors "bubble up" from the low-level components to the user.

### A. CLI / API Errors
1. **Validation Error (Permanent):** return HTTP 400 + a JSON Error. The CLI shows a readable error.
2. **Infrastructure Error (Transient):** the CLI retries (up to 3 times). If that did not work, show "System unavailable, try again later".

### B. Worker Errors (Async)
1. **Crash/OOM:** `worker-manager` catches an exit code != 0 (through Docker events).
   - Publishes the result to the output queue: `status="failed", error="Process crashed"`.
2. **Logic Error (in container):** the agent catches an exception.
   - Publishes the result: `status="failed", error="Exception message"`.

### C. Consumer Errors (Redis)

All consumers use unified `RedisStreamClient.consume()` API with two ACK modes:

**Manual ACK (`auto_ack=False`)** — used by most consumers:
1. The message is read but not ACKed automatically.
2. The consumer processes the message.
3. On success — `await client.ack(stream, group, msg.message_id)`.
4. On an error the ACK is not called and the message stays in the PEL.

**Auto ACK (`auto_ack=True`)** — for fire-and-forget (ProactiveListener, ProvisionerNotifier):
1. The message is ACKed immediately on read.
2. Losing it on a crash is acceptable (notifications, not critical data).

**PEL Recovery** (`claim_pending=True`):
- On startup the consumer calls `XAUTOCLAIM` and picks up messages that have been stuck in the PEL longer than `pending_timeout_ms` (default: 60s).
- This covers the scenario of a consumer crashing mid-processing — after a restart the message is reprocessed automatically.
- PEL recovery runs before the main `XREADGROUP` loop.

**Error handling flow:**
1. **Processing Error (Transient):** we do not call ACK → the message stays in the PEL → PEL recovery picks it up on a restart.
2. **Processing Error (Permanent):** ACK + XADD to the DLQ (if implemented).
3. **Consumer Crash:** the message is in the PEL → another instance or a restart picks it up through `XAUTOCLAIM`.

2. **DLQ Handling:** a separate process or the admin goes through the DLQ by hand.

---

## 5. Deploy Error Handling

### Deploy Retry Limit
Deploy worker writes a typed `DeployOutcome` to `run.result`. Environment-contract failures keep
their specific outcome; unclassified subgraph and smoke failures produce `RETRY`. The supervisor
(`supervise_deploying_stories()` in scheduler) reads the outcome and routes accordingly. After
**3 consecutive RETRY outcomes**, the supervisor transitions the story to `failed`. This prevents
the infinite deploy→fail→redispatch loop.

### Deploy→Engineering Feedback Loop
The supervisor still accepts legacy `CODE_FIX` and `SMOKE_FAILURE` outcomes by creating a fix task
and dispatching it to `engineering:queue`. The current deploy worker does not infer those outcomes:
unknown failures use the bounded `RETRY` path. A future remediation agent may diagnose failed runs
asynchronously and propose a tested code fix outside the deploy path.

### Deploy Deduplication
Atomic `SET NX` Redis lock per project prevents duplicate deploys. Replaces the non-atomic DB-based check that had a race window. Lock held for duration of deploy, released in `finally` block.

### Stale Worker Cleanup
`_check_project_lock()` in the engineering consumer verifies `worker:status` in Redis. Workers in terminal states (`DEAD`/`FAILED`/`STOPPED`) get their Redis keys cleaned up automatically, unblocking new task dispatch without manual intervention.

### Resource Allocation Capacity
Typed allocation failures for insufficient free or reserved RAM park the task in `waiting_resources`, rather than consuming an engineering retry. The scheduler resumes it after fresh server metrics satisfy the same conservative RAM and disk admission checks, and moves it to human review after the configured wait timeout. A request that exceeds every managed server is escalated immediately. `no_fresh_metrics` means the platform cannot evaluate its own fleet: it escalates too, with an operator alert and no owner-facing message, because neither waiting nor retrying the code can end it.

### An Allocation Refusal Never Terminates a Story
Every member of `AllocationFailureReason` is a statement about the platform's servers, never about the user's project, so none of them may fail a story or raise a product-failure alert. That decision lives once, in `shared/allocation_disposition.py::attempt_disposition`, which classifies a failed attempt as `INFRASTRUCTURE_WAIT`, `OPERATOR_REVIEW`, `TECHNICAL_FAILURE` or `PRODUCT_FAILURE`, and states the precedence: when one attempt carries both an allocation refusal and a product failure, the allocation refusal wins. Neither routing path keeps a reason list of its own — the engineering path (`_park_task_waiting_resources`) and the deploy path (`consumers/deploy.py::_record_infrastructure_wait` producing the outcome, `supervise_deploying_stories` routing it) both call that function. On the deploy path the refusal is recorded as `DeployOutcome.WAITING_INFRASTRUCTURE` with its reason and admission budget instead of `GIVE_UP`.

### One Behaviour Per Disposition, On Each Path
The dispositions exist because they need different handling, so no path may answer two of them the same way — a path that does has silently deleted one. `shared/allocation_disposition.py::REFUSAL_ROUTING` is the disposition × path table that states each behaviour once, and both routers branch on it: `_park_task_waiting_resources` (engineering) and `_route_refused_deploy` (deploy).

| Disposition | Engineering | Deploy |
| --- | --- | --- |
| `INFRASTRUCTURE_WAIT` | park in `waiting_resources`, resume when a target is admissible | story stays DEPLOYING, re-dispatched when a target is admissible |
| `OPERATOR_REVIEW` | task and story to human review, operators alerted, owner told it needs an operator | story to human review, operators alerted, owner told it needs an operator |
| `TECHNICAL_FAILURE` | task and story to human review, operators alerted, owner not told | story to human review, operators alerted, owner not told |
| `PRODUCT_FAILURE` | the caller's own failure routing — the only path that may end a story | not reachable from a refusal; a table that said otherwise escalates to a human and logs `deploy_refusal_misclassified` |
| `NONE` | nothing failed in a way this table describes | same |

Both waits are bounded by `supervisor.resource_wait_timeout_minutes`, after which a human is told; the deploy wait carries its start in `run_metadata.infrastructure_wait_started_at` so re-dispatching does not reset the bound. Escalation always means the `human-review` story action — the status value is not a route — and never `fail_story`.

### Target Admission (Provisioning Readiness)
Before capacity is considered at all, a server has to be an admissible target: managed, operational, `labels.provisioning_phase == "complete"`, and free of an active `PROVISIONING_FAILED` incident. The rule is fail-closed — a missing, empty or unknown phase counts as unfinished — and lives once in `shared/server_admission.py`. Both admission paths call it: `_find_suitable_server` (langgraph allocator) and `_resources_available` (scheduler resource wait), so a parked task can never wake up towards a server the allocator would refuse. When no host is admissible for this reason, the allocator raises `AllocationFailureReason.SERVER_NOT_PROVISIONED`: the task parks in `waiting_resources` like a capacity wait — no engineering retry, no story failure, no product-failure notification — but the owner is told through the `task_waiting_infrastructure` PO event, never as a capacity shortage.

### Proactive Message Spam Filter
PO sends user-facing lifecycle messages through `po:proactive`: deploy success, permanent story failure, and resource-wait entry, escalation, and resumption. Intermediate smoke, precheck, and workflow failures stay internal.

---

## 6. Dead Letter Queue (DLQ)

**Naming Convention:** `{original_queue}:dlq`
- `engineering:queue:dlq`
- `deploy:queue:dlq`

**When to send to DLQ:**
1. The message is invalid (does not parse with Pydantic).
2. The retries for Transient errors are exhausted.
3. A logic error that the service cannot handle.

**Payload:**
A copy of the original message + the error metadata:
```json
{
  "original_message": {...},
  "error_context": {
    "error": "ValueError: Invalid project_id",
    "timestamp": "...",
    "service": "langgraph",
    "attempts": 3
  }
}
```
