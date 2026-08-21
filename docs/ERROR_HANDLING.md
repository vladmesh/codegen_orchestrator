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

All consumers read through the unified `RedisStreamClient.consume()` / `consume_typed()` API with two ACK modes:

**Manual ACK (`auto_ack=False`)** — used by most consumers:
1. The message is read but not ACKed automatically.
2. The consumer processes the message.
3. On success — `await client.ack(stream, group, msg.message_id)`.
4. On an error the ACK is not called and the message stays in the PEL.

**Auto ACK (`auto_ack=True`)** — for fire-and-forget (ProactiveListener, ProvisionerNotifier):
1. The message is ACKed immediately on read.
2. Losing it on a crash is acceptable (notifications, not critical data).

**PEL Recovery** (`claim_pending=True`):
- The `XAUTOCLAIM` sweep runs *inside* the read loop, not once before it: `_iter_entries` alternates a sweep with the blocking `XREADGROUP`, so an entry that gets stuck long after start-up is reclaimed by the running consumer, with no restart.
- A sweep claims only entries nobody has been handed for `pending_timeout_ms` (default: 60s). That idle bar is the whole protection against taking work away from a healthy consumer, and the periodic sweep passes it through unchanged.
- A sweep walks the PEL to its end. `XAUTOCLAIM` stops scanning after about `COUNT * 10` entries, so a page whose entries are all still in flight with a healthy consumer answers with an advanced cursor, nothing claimed and no deleted ids. The sweep follows that cursor and stops only when the cursor is terminal (`0-0`) or stops moving. Ending the sweep on the first empty page instead would strand every stale entry behind a fresh prefix, and since each sweep restarts at `0-0` it would walk into the same prefix again for as long as that prefix stays fresh.
- Sweep period: `reclaim_interval_ms`, defaulting to `pending_timeout_ms` floored at `MIN_RECLAIM_INTERVAL_MS` (1s). An entry cannot become claimable sooner than `pending_timeout_ms` after its last delivery, so sweeping faster buys nothing but round trips; sweeping at that period bounds the pickup delay at twice the timeout. The floor exists because a caller may pass `pending_timeout_ms=0` (the proactive listener does) and a zero period would put an `XAUTOCLAIM` on every turn of the loop.
- A consumer that would rather not wait can pass `reclaim_interval_ms` explicitly.
- **A consumer that dispatches concurrently owes the sweep a lease.** Most consumers process an entry inline, so an entry is only pending while the read loop is inside the handler and `pending_timeout_ms` alone tells a stuck entry from a live one. The PO consumer does not: it hands each entry to an `asyncio.Task` and ACKs in that task's `finally`, so an entry is legitimately pending for as long as the graph runs, and a sweep at `min_idle_time = pending_timeout_ms` would hand the process back work it is running. PO answers that with two things and no second mechanism — `RECLAIM_INTERVAL_MS = PEL_TIMEOUT_MS // 2`, so every sweep re-claims (and thereby renews) its own in-flight entries strictly before the idle bar they are measured against, and a set of the ids currently in flight in this process, which are recognised on the way back in and not dispatched again. Liveness is therefore a sign of work, not a reading of the clock: renewal stops when the process stops, and one `PEL_TIMEOUT_MS` later the entry is claimable by another PO.

**Entry lost to a trim:**
Every publish carries `MAXLEN ~ 1000` and the scheduler additionally runs `XTRIM MINID` by age; neither looks at the PEL first. `XAUTOCLAIM` then reports a pending entry whose body is gone — as `(id, None)` on Redis 6.2, in the third response element on Redis 7. Both shapes are logged as `stream_entry_lost_to_trim` and counted in the Redis hash `stream:diagnostics:lost_entries`, keyed `{stream}|{group}` (`RedisStreamClient.lost_entry_count`). The work itself is unrecoverable; what the counter buys is that a trim eating live work does not read as an idle queue.

**Error handling flow:**
1. **Processing Error (Transient):** we do not call ACK → the message stays in the PEL → the running consumer's next sweep picks it up, restart or no restart.
2. **Processing Error (Permanent) — a poison entry:** in `consume_typed`, an entry that cannot be JSON decoded or fails schema validation is copied to `{stream}:dlq` and ACKed away *only after that copy lands*. If the DLQ write fails, the entry is left unacked and comes back on a later sweep — a message is never destroyed because its diagnostics copy failed.
3. **Newer publisher:** a payload that fails validation *only* because it carries fields this consumer's schema does not know yet is not poison. It is accepted with those fields dropped and `typed_consume_unknown_fields_ignored` logged (field names only). Anything else — a missing field, a wrong type, a `QueueMeta.version` this consumer does not implement — is not forgiven and takes the DLQ route above. Tolerance is read-side only: the contracts keep `extra="forbid"`, so a publisher still cannot emit an unknown field.
4. **Consumer Crash:** the message is in the PEL → another instance or a restart picks it up through `XAUTOCLAIM`.
5. **DLQ Handling:** a separate process or the admin goes through the DLQ by hand.

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
Before capacity is considered at all, a server has to be an admissible target: managed, operational, `labels.provisioning_phase == "complete"`, and free of an active `PROVISIONING_FAILED` incident. The rule is fail-closed — a missing, empty or unknown phase counts as unfinished — and lives once in `shared/server_admission.py`. Every path that places a workload calls it: `_find_suitable_server` (langgraph allocator, a new host), `_refuse_inadmissible_target` (the same module's reuse branch, the host a project is already bound to) and `_resources_available` (scheduler resource wait), so a parked task can never wake up towards a server the allocator would refuse, and a redeploy or a newly added module cannot be placed on a host that is no longer a legal target. When no host is admissible, the allocator raises `shared/server_admission.py::ADMISSION_FAILURE_REASON` — `AllocationFailureReason.SERVER_NOT_PROVISIONED`: the task parks in `waiting_resources` like a capacity wait — no engineering retry, no story failure, no product-failure notification — but the owner is told through the `task_waiting_infrastructure` PO event, never as a capacity shortage.

That one reason covers all four rejections, on both placement paths, and there is one constant rather than a rejection-to-reason table because there is no branch to make: none of the rejections is a statement about how much memory was asked for. Two of them — the host is not managed, or its status does not admit — are not literally an unfinished build and the reason vocabulary has no member for them; they are still platform state, and the alternative to the closest infrastructure reason is describing them to the owner as a capacity shortage, which is false. A subset that held only the two provisioning rejections was how the two paths drifted apart: the search path let a host merely in status `provisioning` fall through to `insufficient_free_memory`, which a live acceptance run then read back on an empty 4 GB machine.

Reuse is placement, so it is admitted like placement: `ensure_project_allocations()` applies the predicate to the bound server before it returns existing allocations and before it allocates a port for a new module, and refuses with the same constant.

One question is answered ahead of that reason, and only in the search path: whether any managed server could fit the request even fully admitted. If none could, the refusal stays `IMPOSSIBLE_CAPACITY` with `OPERATOR_REVIEW` although a host was also refused admission — finishing that host's provisioning would not make it bigger, so an infrastructure wait would park the request on an event that never arrives, while an operator can be told at once that the fleet has no machine of the required size. That is not a host's state retold as a memory shortage; it is a separate durable fact about the fleet, and the bound-host path never asks it because it has one host and no alternative to compare it with.

A bound-host refusal also shapes the wait: resuming asks whether *any* server is admissible, while this project is refused by *the one it sits on*, so a fleet with one healthy host and one broken host the project is pinned to satisfies the resume condition on every tick and is refused again on every tick. Both waits check their elapsed-time bound before admissibility, which ends that cycle in the same escalation as a wait with no target at all. A server-pinned resume condition would end it sooner, but the wait's contract is fleet-wide today and the bound is what makes the cycle finite.

### Proactive Message Spam Filter
PO sends user-facing lifecycle messages through `po:proactive`: deploy success, permanent story failure, and resource-wait entry, escalation, and resumption. Intermediate smoke, precheck, and workflow failures stay internal.

---

## 6. Dead Letter Queue (DLQ)

**Naming Convention:** `{original_queue}:dlq`
- `engineering:queue:dlq`
- `deploy:queue:dlq`
- `po:input:dlq`

**Implemented for:** the typed consume path, which is what the PO consumer reads through too. `RedisStreamClient.consume_typed` writes every entry it refuses — bad JSON, failed validation, a message still addressed by the removed `user_id` — to `{stream}:dlq` before ACKing it. Nothing else writes to a DLQ yet: exhausted transient retries and service-level logic errors are handled by the owning consumer and do **not** produce a DLQ entry today.

**Payload:** flat stream fields, not a nested JSON document:

| Field | Meaning |
|-------|---------|
| `source_stream` | the stream the entry came from |
| `group` | the consumer group that refused it |
| `entry_id` | its id on the source stream |
| `failure` | `decode_error` or `validation_error` |
| `reason` | JSON: for a validation failure the `{type, loc}` list with every input value elided; for a decode failure the positional `JSONDecodeError` text |
| `quarantined_at` | UTC ISO-8601 |
| `body` | JSON of the original field map, verbatim |

**Secrets:** `body` is kept whole, secrets included. That is a deliberate difference from the logs, and the two must not be conflated. A DLQ entry is a stream in the same Redis, under the same credentials, next to the stream the payload already sat on in cleartext — copying it there crosses no trust boundary. Logs go to Loki and are read by a wider audience, which is why `reason` elides values, `str(e)` is never logged for a validation error, and the raw field map is never logged at all. Do not "improve" the DLQ by logging its body.

The DLQ stream is written with the same `MAXLEN ~` as any other publish, so a flood of poison entries cannot grow it without bound; a DLQ that is being trimmed is itself a signal to go look.
