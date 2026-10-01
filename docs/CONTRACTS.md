# Contracts

This is the registry for REST and Redis boundaries. It describes ownership,
delivery, and lifecycle rules that are not apparent from a type declaration.
Field-level definitions live in `shared/contracts/**`; API-only schemas live in
`services/api/src/schemas/**`. Edit those sources, then update this registry if
the boundary, producer, consumer, or invariant changes.

## Design principles

1. **One schema definition.** Request and shared response models are defined in
   `shared/contracts/`; API schema modules import or re-export them. A request
   must be valid for both caller and API.
2. **Typed boundaries.** REST payloads and Redis messages are validated at the
   boundary. A consumer must not infer a missing field from current state.
3. **Logical ownership.** Producers and consumers below name the actor that
   makes the business decision, not a transport implementation detail.
4. **Traceability.** Queue messages carry correlation metadata from
   `shared/contracts/base.py`; worker turns additionally use their broker-owned
   request id.
5. **Fail closed.** Missing ownership, unresolved recipients, malformed typed
   results, and incomplete infrastructure observations are explicit outcomes,
   never successful defaults.

## Boundary guides

Feature-specific invariants live in focused guides. Read this index first, then only the guide for the boundary you are changing.

| Boundary | Canonical guide |
|---|---|
| Story PR conflict repair | [PR conflict repair contracts](contracts/pr-conflict-repair.md) |
| Generated-service user grants and deploy redaction | [Generated-service grants and deploy redaction](contracts/generated-service-grants.md) |
| Engineering attempts, work admission, and budgets | [Work admission and engineering attempts](contracts/work-admission.md) |
| Product Brief coverage and dispatch | [Product Brief contract](contracts/product-brief.md) |
| Project/repository and Story/Task/Run REST surfaces | [REST story, task, run, and policy surfaces](contracts/story-task-run-surfaces.md) |
| Generated-product template/package installation and QA | [Generated product kit and QA contracts](contracts/kit-template-and-qa.md) |
| Runtime lifecycle/security invariants | [Lifecycle and security invariants](contracts/lifecycle-invariants.md) |
| Managed target reconciliation/readiness | [Managed target readiness](contracts/managed-target-readiness.md) |

## Canonical vocabularies

`shared/contracts/vocab.py` owns cross-boundary enums such as `AgentType`,
actor and notification vocabularies. Status and domain vocabularies closest to
their models are listed in the DTO registry below. Consumers must import these
symbols rather than compare locally invented strings.

## Queue registry

`shared/queues.py` owns stream/group topology. Message definitions and their
serialization live under `shared/contracts/queues/`. The table is a routing
registry, not a duplicate of message fields. In registry tables, `dto/...` and
`queues/...` paths are relative to `shared/contracts/`; all other source paths
are repository-relative.

| Stream / pattern | Group | Message source | Logical producer | Consumer |
|---|---|---|---|---|
| `scaffold:queue` | `scaffold-consumers` | `queues/scaffold.py` | scheduler-pipeline | scaffolder |
| `architect:queue` | `architect-consumers` | `queues/architect.py` | PO/API story action | architect consumer |
| `engineering:queue` | `capability-workers` | `queues/engineering.py` | scheduler-pipeline | langgraph engineering consumer |
| `deploy:queue` | `capability-workers` | `queues/deploy.py` | scheduler-pipeline or API action | langgraph deploy consumer |
| `qa:queue` | `qa-consumers` | `queues/qa.py` | deploy supervisor or admin action | langgraph QA consumer |
| `worker:commands` | `worker_manager` | `queues/worker.py` | langgraph | worker-manager |
| `worker:responses:developer` | response stream | `queues/worker.py` | worker-manager | langgraph |
| `worker:{worker_id}:input` | broker session | producer JSON envelope (`worker_spawner.py` / `qa_worker.py`) | developer/QA node | worker-wrapper/broker |
| `worker:{worker_id}:output` | broker session | wrapper output envelope | worker-wrapper/broker | developer/QA node |
| `provisioner:queue` | `infrastructure-workers` | `queues/provisioner.py` | scheduler-infrastructure | infra-service |
| `provisioner:results` | scheduler / bot groups | `queues/provisioner.py` | infra-service | scheduler-infrastructure, telegram-bot |
| `po:input` | `po-consumer` | `queues/po.py` | bot and system producers | PO consumer |
| `po:response:{request_id}` | direct response | `queues/po.py` | PO consumer | telegram-bot |
| `po:proactive` | `tg-bot-proactive` | `queues/po.py` | PO notification tools | telegram-bot |

### Recipient and PO rules

Telegram admits bounded concurrent updates through PTB's native update processor.
`TELEGRAM_MAX_CONCURRENT_UPDATES` is required and at least two. The processor holds
a FIFO asyncio lock for the effective user across authorization and all registered
handlers, including commands and callbacks. Different users have distinct contexts
and locks; an idle lock is removed when its last admitted waiter leaves. PTB owns
admission and shutdown, so this adds no scheduler or persistent user registry.
PO's existing per-chat graph serialization and protected request streams remain
the downstream boundary.

Canonical sources: `shared/contracts/recipient.py` and
`shared/contracts/queues/po.py`.

Addressable messages use `telegram_chat_id`; an internal `User.id` is never a
delivery address. A producer resolves the chat before publication and escalates
an unresolved recipient. `DeployMessage` requires exactly one of an address or
an `unaddressed_reason`. Legacy ambiguous `user_id` payloads are rejected,
quarantined to DLQ, and alerted rather than silently becoming unaddressable.

The PO prompt's "Story Events & Reminders" lists the events PO receives, each an
`OwnerNotificationEvent`: `story_completed`, `story_failed`, `story_blocked`,
`story_quarantined` (worded as `story_blocked`: work is stopped, a person decides, no
known time), `story_impossible_capacity`, `task_impossible_capacity`,
`story_waiting_user_secret` and `story_requirements_returned`. A unit test
holds the listed set inside the vocabulary the consumer routes.

A `story_stage` event carries `stage`, `waiting_on`, `wait_estimate`, `stage_notice`,
`stage_notice_step` and `stage_entered_at`. The step is 0 for `entered` and only for it, `n` for the
`n`-th `still_there` of the stay. `stage_entered_at` names the stay: when its entry notice went out,
the same on every notice of the stay, later for a return to the stage. The PO consumer logs and
drops every stage notice before the graph; the scheduler producer remains unchanged.

At the single proactive publish point, `consumers/po_story_gate.py` classifies the current API
story as `in_work`, `needs_user`, `stopped`, `completed` or `failed`. Reminders publish only an
untold `needs_user` or `stopped` state; a planning failure that parked the story is stopped, while a
planning being retried automatically, changes among in-work statuses, resource waits and escalation
steps never justify a message. A reminder names its story in `story_id` (the explicit argument of
`set_reminder`); one naming no story publishes only if `user_requested` is true, i.e. it was set in
the user's own turn. Terminal
reminders stay silent because the durable seam tells endings. Durable key events and returned
requirements publish and record what was told. Resource/infrastructure waits and resumptions run
the PO turn but their replies are suppressed. Redis retains the last told state per chat/story
without expiry until the story ends; the previous fingerprint format counts as already told.
An unreadable gate suppresses reminders, while durable events still publish.

**Only an ordered story's outcome reaches the user.** A story is *ordered* when
`GET /api/product-briefs/by-story/{id}` returns a brief with `confirmed_at` set. The PO consumer
checks it before the PO graph for every remaining `system_event` that names a story, so producers do not: a
not-ordered story's event never reaches the graph or `po:proactive`, the admins get it marked as
withheld. Stage notices were already dropped without an audience read. `story_waiting_user_secret` is
exempt. A reminder that names a story passes the same check at the same entry: a not-ordered
story's reminder runs no PO turn and is logged. A 404 or a validated unconfirmed brief is "not
ordered"; every exception while reading the brief, including malformed bodies, leaves the entry
unacked for the PEL sweep to hand back.

**The situation snapshot.** Every `system_event` turn that reaches the PO graph carries a snapshot
built by `agents/po/situation.py` from existing API reads and the chat's
`po:last_user_message:<chat>` key (written on each user turn): the order (story and brief
`confirmed_at`, or "not an order"), the story's status and when it entered it
(`status_entered_at`; null on rows landed before that column existed reads `unknown`, never
`updated_at`), for a stopped story (key state `stopped`) the fixed fact "stopped, a person is
needed, no known deadline", for a story the state-age watchdog stopped the wait its
`state_wait_age_bound_exceeded` reason records (marked "stopped in a mass sweep after downtime" when
the reason has `mass_sweep: true`), the user's last message, the project's Application status and last health
check, the user's other ordered stories in work, a count of platform work, and a `### Deferred
notices` section (empty until notices are deferred). Dates are absolute UTC plus a human age. Each
field is read on its own: a read that raises, answers 404 or returns a body that is not the DTO
makes that field `unknown`, and the turn runs. The snapshot travels in the run config
(`po_situation`) and the graph's prompt appends it to the system message, so the checkpointer never
stores it. A user turn has none; `get_product_situation(project_id)` returns the same text for one
of the user's own projects, about its current or latest ordered story.

In a PO turn without `request_id` (a reminder or system event) the only way to the user is that
gated final reply: the `notify_user` tool publishes nothing there (the consumer passes
`user_turn` in the run config). No other PO tool publishes to `po:proactive`, and a unit test
holds that.

PO models use the logical flat-field codec from `queues/po.py`. Before the first
Redis command, `RedisStreamClient.publish_flat` (and `publish`/`publish_message`
on PO streams) protects the entire payload with the deployed
`SECRETS_ENCRYPTION_KEY`. The sole wire field is `po_encrypted_v1`, a Fernet token
authenticating both the destination key and the payload. No model field remains
in cleartext. `consume_typed`, the proactive `consume` path, and the bot's direct
response XREAD authenticate/decode before validation or delivery. Runtime has no
plaintext read/write fallback. PO and bot startup authenticate retained PO
payloads and refuse released plaintext before consumption; the offline,
quiesced converter is described in [PO Redis production runbook](runbooks/po-redis-and-checkpoints.md#production-po-redis-upgrade).

PO reminders protect the entire JSON member before ZADD; the poller authenticates
before validating and publishing a separately protected input. Latest owner
events protect the entire JSON document before SET; notice tools authenticate
before reading the notice reference. Keys, stream entry IDs, consumer/group
names, cursors, delivery counts, reminder scores and TTLs remain operational
metadata. Payload timestamps, names, reasons, QA facts, notice references, errors,
and recipient/story/project/task identifiers are inside the envelope. A failed
reminder authentication retains the member for repair and logs no body.

PO quarantine protects the complete DLQ record, including the original wire
body and failure reason, before XADD and ACK. A failed DLQ write leaves the
original entry pending. Validation logs report counts; transport exceptions
report safe classifications. Unvalidated alert identifiers are restricted to
known event vocabulary and identifier shapes. Payloads and exception bodies are
never rendered in transport failure logs or alerts. ACKed retained entries remain
protected. Other services' queue representations are unchanged.

The proactive listener
acks only after successful delivery or terminal delivery exhaustion. Its PEL
delivery count survives a restart; exhaustion is alerted and is not retried as
an endlessly valid message.

Every text the bot sends a user on the reply and proactive paths goes through one
function, `send_text` in `services/telegram_bot/src/proactive.py`. It splits on
`MESSAGE_BREAK` (`queues/po.py`, ASCII RS `\x1e`): a producer that wants a new Telegram
message puts it into `POResponse.text` or `POProactiveMessage.text`, never inside an
open HTML tag, and the bot drops it and any empty part. A part over
`SAFE_MESSAGE_LENGTH` (4000 UTF-16 units of HTML source, `shared/telegram_text.py`) is
cut at the last paragraph, then line, then word boundary that fits, never inside a tag
or entity; tags open at a cut are closed and reopened, so every chunk is valid HTML.
Chunks go in order, each HTML first and plain text if refused. A proactive retry
resumes at the chunk that failed (`SendProgress`, kept per delivery). A failed reply
gets a fixed apology, never exception text.

### Worker command and turn rules

Canonical control-plane sources: `shared/contracts/queues/worker.py`,
`shared/contracts/worker_control_plane.py`, and `shared/contracts/worker_turn.py`.
Per-worker input is the shared `WorkerTurnInput` envelope. Engineering producers
must send `attempt_id` together with `turn_deadline_seconds`; QA executor turns
omit both and therefore do not create engineering active-turn supervision state.
The broker rejects unknown or malformed turn fields before leasing them, and the
wrapper client revalidates the normalized payload before execution.

Worker-manager owns container lifecycle. Developer turn I/O bypasses
worker-manager: the wrapper/broker leases input, accepts one typed output, and
acks only after that output is accepted. Session streams have bounded retention
and a finite broker TTL. Authentication to the wrapper is not authorization:
the broker and manager each enforce the recorded worker type. QA workers have
the constrained QA turn capability; they cannot obtain Compose control.

## Consumer patterns

<a id="consumer-patterns"></a>

`shared/redis/client.py` is the common stream boundary. Consumers use its typed
helpers and must classify the entry before acknowledging it.

| Situation | Required handling |
|---|---|
| normal typed success | complete the owned durable work, then ACK |
| malformed or permanently invalid payload | alert/quarantine to `{stream}:dlq`, then ACK |
| transient failure | leave pending for retry/reclaim; do not ACK |
| restart or abandoned consumer | reclaim compatible PEL entries through the configured claim path |
| trimmed or missing pending entry | treat as a bounded recovery fact, not a successful completion |

Delivery is at least once. Consumers that classify repeated processing failures
for retry use a durable PEL delivery ceiling (five deliveries by default unless
the queue declares a narrower bound); once exhausted, the entry is quarantined
to the same DLQ before ACK instead of being executed forever. Explicit durable
state waits are not processing failures and keep their queue-specific settlement
contract. Cancellation never spends the retry budget: it propagates and leaves
the entry pending for the normal ownership/reclaim path.

Consumer idempotency belongs to the durable owner:
a database lock, persisted request/run identity, or explicit turn/adoption key.
`XAUTOCLAIM` recovery, DLQ handling, and approximate stream trimming do not
authorise a consumer to invent a result. The PO consumer additionally tracks
its in-flight ids so one process does not reclaim its own active dispatch.

Capability executions retain the released live-work keys, 60-second lease and
Redis-server-time expiry. Renewal atomically refuses expired tokens, even before
PEL liveness pruning. The existing 10-second refresh cadence also bounds each
network attempt and the delay between transient connection/timeout retries. A
local monotonic deadline starts before the last confirmed acquisition/renewal
request; failed attempts never extend it. A ten-second connection outage fits
inside this confirmed window. No renewal is accepted after the local deadline.
Confirmed teardown still cancels the owner. Missing/expired ownership, exhausted
uncertainty and non-transient errors cancel visibly and retain the pending entry.
Leased completion and cancellation use one terminal Lua decision: check the
completed result's settlement, teardown fence and unexpired token against Redis
TIME, then XACK atomically. Every retry repeats this decision inside the original
confirmed deadline. A returned result remains authoritative through subsequent
owner cancellation; only a running process that unwound can settle as cancelled
work. Unproven external cancellation, unsettled teardown results, known ownership
loss and exhausted uncertainty prevent ACK. Cancellation without teardown leaves
the entry pending. An observed teardown continues to constrain a returned result
even if its key later disappears. Shutdown settlement is additionally bounded by
the existing ten-second attempt window. A lost ACK reply may mean Redis already
committed: a subsequent zero XACK is unproven, fences cleanup and never claims
recovered settlement.
No durable ACK recovery protocol is introduced. A definitive ACK reply received
after the conservative local deadline is reported as already applied but cannot
claim current ownership. Watchers are cancelled and awaited on every exit; bounded
best-effort failure marking and lease removal cannot mask the primary failure.
This changes no DTO, stream, schema, key format or released-worker upgrade protocol.

## Current flow map

### Engineering

1. The bot publishes a PO input; the PO/API path creates project and story
   records through typed REST schemas.
2. Scheduler publishes scaffold and architect work when their durable state is
   ready, then publishes unblocked engineering work.
3. The engineering consumer loads the persisted executor decision, creates a
   worker through worker-manager, and drives the developer broker turn.
4. Terminal writing settles the Run and ledger under their ownership rules;
   downstream deploy routing reads typed terminal data.

### Deploy and QA

1. **A commit deploy names two commits and never conflates them.** `head_sha` is
   what the story produced — the pull request head — and story evidence, the
   access grants and the redundant-deploy key (`Deployment.deployed_sha`) are all
   keyed on it. `deployed_commit_sha` is the built commit on the default branch:
   the project's CI tagged its images with it, the checkout is pinned to it, and
   `Deployment.deployment_info.deployed_commit_sha` records it. No merge method
   makes a branch's new head equal the pull request head, so the two differ on
   every PR-merge deploy. Both travel on `DeployMessage` and in the deploy Run's
   metadata; the resolver refuses to name images without the second.
2. **No deploy Run exists until that commit's images are observed published.**
   The producer waits, bounded at 15 minutes from the merge and fail-closed, for
   the project's own `ci.yml` run for the built commit on its default branch —
   that run's `build-and-push` is the publication. Nothing retriggers or repairs
   it. A run that will never publish refuses at once; the bound refuses the rest.
   Every observation is persisted in `Story.generated_product_timeline` with the
   PR identity and every distinct CI run id, status and conclusion seen through
   the GitHub App. Its missed-capture list is recomputed from the best merged
   observation: later positive PR, run, job, step or log facts clear earlier
   misses, and recovered failure details clear their unavailability reason. A
   refusal creates no Run, so its typed reason lands on the
   story (`quarantine_reason.deploy_outcome = images_not_published`, with the
   commits, CI run, failed jobs and steps, and redacted bounded log evidence)
   before `POST /api/stories/{id}/human-review` parks it for human review.
3. A durable deploy Run is created once that is true, before `DeployMessage`
   publication. Inside the deploy, before any external effect, the deployer reads
   the registry once for exactly the `*_IMAGE` references it resolved: absent
   images — the registry's `404`, the only status that answers the question —
   are `IMAGES_NOT_PUBLISHED`, and a registry that cannot be read at all,
   whether it is unreachable or answers any other error status, is
   `IMAGE_REGISTRY_UNREADABLE`, which is a separate outcome because "not asked"
   is not an answer about the project. **A deploy Run's SUCCESS is a claim
   about images, not about a SHA**: it names the references and digests it
   deployed in `deployment_result`, and the service-deployment record carries the
   same.
4. The deploy consumer records the dispatch boundary before GitHub Actions is
   no longer safely stoppable, then writes its typed terminal result.
5. The supervisor reads that typed result, creates QA work only with resolved
   repository criteria, and routes the typed QA outcome.
6. A terminal owner notification is persisted before it is published to PO, and
   a lifecycle-wait notice (resource wait, resume, secret ask) is persisted in
   the transaction of the move it announces. Recovery retries the owned
   notification record; it does not duplicate an already settled owner event.

## REST DTO registry

Field-level definitions live in `shared/contracts/**` and API-only schemas in
`services/api/src/schemas/**`. The canonical registry and boundary-specific
invariants are in [REST story, task, run, and policy surfaces](contracts/story-task-run-surfaces.md).

## Queue message registry

<a id="scaffoldmessage"></a>

| Message / result family | Canonical source | Producers | Consumers | Delivery and ownership rule |
|---|---|---|---|---|
| `ScaffoldMessage` | `queues/scaffold.py` | scheduler-pipeline | scaffolder | scaffold durable state is claimed before work and settled through typed result paths |
| `ArchitectMessage` | `queues/architect.py` | PO/API and scheduler-pipeline | architect consumer | story identity, not conversational state, drives decomposition |
| `EngineeringMessage` | `queues/engineering.py` | scheduler-pipeline | engineering consumer | task id names the immutable paid Run decision; initiating run id fences worker ownership |
| `DeployMessage`, triggers/actions/outcomes | `queues/deploy.py` | scheduler-pipeline/API | deploy consumer | recipient rule is address xor reason; terminal result belongs to deploy Run owner |
| `QAMessage`, QA outcomes | `queues/qa.py` | supervisor/admin action | QA consumer | run id names the QA decision; criteria are resolved before publication |
| worker commands/responses | `queues/worker.py` | langgraph / worker-manager | worker-manager / langgraph | only lifecycle owner creates, deletes, or answers a worker command |
| worker input/output | `shared/contracts/worker_turn.py`, LangGraph worker clients, wrapper/broker | developer/QA node | wrapper / developer/QA node | `WorkerTurnInput` is validated before lease; engineering turns carry attempt+deadline together, while QA turns omit both; broker request id and accepted output settle a leased turn |
| provisioning request/result | `queues/provisioner.py` | scheduler-infrastructure / infra-service | infra-service / scheduler-infrastructure and bot | result consumers use their own group semantics |
| PO input/response/proactive | `queues/po.py` | bot/system/PO | PO/bot | flat codec and recipient validation apply before consumption |
| progress event | `events.py` | services | bot | progress does not authorise state transition |

Every developer and QA executor is owned by a project, initiating Run, and
attempt. Story-scoped engineering and QA producers additionally require and
carry their real `story_id`; worker-manager writes that value to
`worker:meta:<id>` and `com.codegen.story.id` before the container exists.
Released storyless tasks and ad-hoc administrative E2E runs remain explicitly
run-owned: their message and `WorkerOwnership.story_id` are `None`, and no story
metadata or Docker label is invented.
The scheduler reconciles `completed`, `failed`, and `archived` stories every
supervision tick by rediscovering all matching metadata and publishing the
canonical `DeleteWorkerCommand`. One scheduler finalizer retains the
`story:workers` binding until worker-manager has deleted that exact worker's
status and metadata and released its owner-fenced project lock, then
compare-deletes only the unchanged binding. A failed publish, incomplete
removal, or replacement owner remains retryable and blocks handoff. Both
`complete_stories` PR-review routes and terminal reconciliation use this same
order before transition or next-story eligibility.
Canonical teardown also unbinds the story itself: `delete_worker` compare-deletes
the `story:workers` entry that still names the worker it is removing, before it
deletes that worker's status and metadata. Reuse requires the same evidence from
the other side — a binding whose `worker:status:<id>` and `worker:meta:<id>` are
both absent names a removed worker, so the registry evicts it and the caller
spawns. A present but unrecognised status stays inconclusive and is still reused.

The owner-fenced `workspace:lock:<project>` is repaired during create only when
its worker metadata names a story and an authenticated internal API read proves
that story terminal. Missing ownership, lookup failure, a live story, or a
replacement lock fails closed and the refusal names both known identities.
Worker GC likewise requires a terminal worker status plus a container proven
non-live or absent; a failed Docker inventory is not absence.

For a developer `WorkerCompletedResult`, worker-wrapper is the sole publication
boundary. It first resolves the reported commit, including an unambiguous
abbreviation, and requires it to equal local `HEAD`; it then non-force pushes
that exact `HEAD` to the configured story branch and reads the remote branch ref
back. Only an exact readback publishes `completed`. A wrong checkout branch,
commit mismatch, push failure, or readback mismatch publishes `failed` instead,
and retains the agent's final `content` as `worker_report` when no fuller report
already occupies that diagnostic surface. Credentials and Git stderr never enter
the result.

## Lifecycle and security invariants

Runtime lifecycle and security invariants are split by boundary:

- [Lifecycle and security invariants](contracts/lifecycle-invariants.md)
- [Generated product kit and QA contracts](contracts/kit-template-and-qa.md)
- [Managed target readiness](contracts/managed-target-readiness.md)

## Source map

| Area | Source of truth |
|---|---|
| shared REST DTOs | `shared/contracts/dto/` |
| queue messages and results | `shared/contracts/queues/` |
| shared Redis topology and client semantics | `shared/queues.py`, `shared/redis/client.py` |
| shared run, recipient, worker, and env invariants | `shared/contracts/` |
| what central QA can check, and never does | `shared/contracts/qa_capabilities.py` |
| API-only request/response composition | `services/api/src/schemas/` |
| REST route ownership | `services/api/src/routers/` |
| LangGraph consumers | `services/langgraph/src/consumers/` |
| scheduler publishers/supervision | `services/scheduler/src/` |
| worker lifecycle | `services/worker-manager/src/` |
| infrastructure execution | `services/infra-service/` |

## Contract change checklist

Before changing a DTO, queue, or API boundary:

1. Locate the canonical source from this registry and inspect its producers and
   consumers.
2. Update one source definition rather than adding a same-named API copy.
3. Decide delivery, idempotency, ownership, recipient, and failure semantics.
4. Add behaviour-level tests at the boundary and update this document only for
   a new registry entry or invariant.
5. Do not add compatibility shims, fallbacks, or dual-read paths for data that
   no longer exists. When a persisted row or payload shape changes, migrate the
   data with Alembic instead of keeping a compat branch. Document the current
   rule, not its chronology.
