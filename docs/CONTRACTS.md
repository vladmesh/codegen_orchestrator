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

## Caller principals

`services/api/src/dependencies.py` and `services/api/src/routers/_recipients.py`
resolve the caller once. An LK bearer acts only as the token subject. An
internal key is a service principal and may name an actor through
`X-Telegram-ID`; a bare Telegram header is not authentication. Authorization,
project ownership, allocation administration, and recipient resolution use that
principal rather than an untrusted header lookup.

### Generated-service user grants

Generated Telegram services use their `USERS_GRANT_CAPABILITY` generated secret
only through the deploy resolver's in-memory `secret_values`. A grant intent is
typed `users_grant_intent` metadata on the durable deploy Run. It binds one
verified channel/external identity, initiating actor, and immutable target
application/deployment/SHA without holding secret material. The Run is written
before publish; repeats resume its same intent. After smoke success, deploy
grants through `POST /users/grant` and requires `GET /users/access` to report
that exact identity active before marking the intent applied or reporting access
live. An incoming ownership transfer changes `Project.owner_id` only in the
post-readback transaction that records the applied intent. The capability is
never an override, project configuration value, URL, event field, or log value.

An `INITIAL_OWNER` intent belongs only to the dedicated, stable
`initial_owner_seed` deploy Run whose id is derived from the project and verified
owner identity. It is deduplicated on that pair. QA temporary-access grant or
revoke runs, and supervisor retry, infrastructure-wait, and secret-wait runs,
are ineligible even when they carry a deployment SHA. An `ADD_USER` or
`INCOMING_OWNER` retry may resume only while its immutable target is still the
live deployment; an applied intent short-circuits, and a stale target is
rejected rather than redeployed.

The existing `TG_BOT_TEST_TELEGRAM_ID` temporary-access slot remains a separate
revocable QA lifecycle. `users.grant` has no revoke or expiry protocol, so it
must not be used as a QA TTL grant until the template adds one.

## Accounting, admission, and executor selection

### Engineering attempt ledger

Canonical model: `shared/contracts/dto/engineering_attempt.py`.

`engineering_attempt_ledger` records one terminal coding-agent attempt under
the stable `engineering-run:{run_id}` identity. The terminal Run writer holds
the Run lock while it writes the ledger, so redelivery retains the first fact.
Money is integer micro-USD, never float. Unknown cost is null, not zero; a
provider-reported cost must name both a provider and an amount. A project
deletion detaches relationship ids from accounting history without deleting the
accounting fact.

Provider evidence is parsed only by the worker-wrapper paths that own it. Claude
may contribute its documented terminal result facts; Factory contributes a
single valid result document without money; Codex stdout/stderr is not a usage
or cost source. The ledger validates internally consistent evidence before it
becomes an immutable terminal fact.

### Work admission and budgets

| Surface | Canonical source | API owner |
|---|---|---|
| paid-run command and outcomes | `shared/contracts/dto/work_admission.py` | `services/api/src/routers/work_admission.py` |
| per-user engineering budget policy | `shared/contracts/dto/engineering_budget_policy.py` | `services/api/src/routers/engineering_budget_policies.py` |
| executor decision snapshot | `shared/contracts/dto/executor_decision.py` | `services/api/src/work_admission.py` |
| executor diagnostics snapshot | `shared/contracts/dto/executor_diagnostics.py` | `services/api/src/executor_diagnostics.py` |

The paid-run command locks its controls, evaluates count admission and (for
engineering) money admission, then creates the queued Run in one transaction.
It records an audit decision. A queued or running replay returns the persisted
Run; a terminal identity is not reopened. A publish failure has an unknown
broker outcome, so the committed Run and reservation remain recoverable rather
than being cancelled speculatively.

Admission writes an immutable `executor_decision` before a billable side effect.
Consumers load that decision by the engineering task id or QA run id; they do
not select an executor from mutable project or process configuration. A malformed
control or diagnostic is fail-closed. An administrator may confirm only a
specific unexpired `unknown` diagnostics snapshot; an internal service cannot
make that confirmation.

### Operational overview

`shared/contracts/dto/admin_overview.py` defines the bounded overview payload.
`services/api/src/queue_snapshot.py` owns queue inspection for the overview and
debug route. Missing Redis data is `degraded` or unavailable, never a fabricated
zero. Legacy or invalid executor decisions remain labelled as such and are not
reconstructed from current configuration.

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
| `scaffold:queue` | `scaffold-consumers` | `queues/scaffold.py` | scheduler dispatcher | scaffolder |
| `architect:queue` | `architect-consumers` | `queues/architect.py` | PO/API story action | architect consumer |
| `engineering:queue` | `capability-workers` | `queues/engineering.py` | scheduler dispatcher | langgraph engineering consumer |
| `deploy:queue` | `capability-workers` | `queues/deploy.py` | scheduler or API action | langgraph deploy consumer |
| `qa:queue` | `qa-consumers` | `queues/qa.py` | deploy supervisor or admin action | langgraph QA consumer |
| `worker:commands` | `worker_manager` | `queues/worker.py` | langgraph | worker-manager |
| `worker:responses:developer` | response stream | `queues/worker.py` | worker-manager | langgraph |
| `worker:{worker_id}:input` | broker session | `queues/developer_worker.py` | developer node | worker-wrapper/broker |
| `worker:{worker_id}:output` | broker session | `queues/developer_worker.py` | worker-wrapper/broker | developer node |
| `provisioner:queue` | `infrastructure-workers` | `queues/provisioner.py` | scheduler | infra-service |
| `provisioner:results` | scheduler / bot groups | `queues/provisioner.py` | infra-service | scheduler, telegram-bot |
| `env-observation:queue` | `infrastructure-workers` | `queues/env_observation.py` | temporary-access sweep | infra-service |
| `po:input` | `po-consumer` | `queues/po.py` | bot and system producers | PO consumer |
| `po:response:{request_id}` | direct response | `queues/po.py` | PO consumer | telegram-bot |
| `po:proactive` | `tg-bot-proactive` | `queues/po.py` | PO notification tools | telegram-bot |
| `task_progress:{task_id}` | event stream | `events.py` | services | telegram-bot |

### Recipient and PO rules

Canonical sources: `shared/contracts/recipient.py` and
`shared/contracts/queues/po.py`.

Addressable messages use `telegram_chat_id`; an internal `User.id` is never a
delivery address. A producer resolves the chat before publication and escalates
an unresolved recipient. `DeployMessage` requires exactly one of an address or
an `unaddressed_reason`. Legacy ambiguous `user_id` payloads are rejected,
quarantined to DLQ, and alerted rather than silently becoming unaddressable.

PO streams use the flat-field codec from `queues/po.py`. The proactive listener
acks only after successful delivery or terminal delivery exhaustion. Its PEL
delivery count survives a restart; exhaustion is alerted and is not retried as
an endlessly valid message.

### Worker command and turn rules

Canonical sources: `shared/contracts/queues/worker.py`,
`shared/contracts/queues/developer_worker.py`,
`shared/contracts/worker_control_plane.py`, and
`shared/contracts/worker_turn.py`.

Worker-manager owns container lifecycle. Developer turn I/O bypasses
worker-manager: the wrapper/broker leases input, accepts one typed output, and
acks only after that output is accepted. Session streams have bounded retention
and a finite broker TTL. Authentication to the wrapper is not authorization:
the broker and manager each enforce the recorded worker type. QA workers have
the constrained QA turn capability; they cannot obtain Compose control.

### Infrastructure observations

`shared/contracts/queues/provisioner.py` defines provisioning messages.
`shared/contracts/queues/env_observation.py` defines read-only deployed-env
observations. Infra-service runs the observation on the target and persists its
result under the request id for the documented TTL; it does not send the result
through another queue. The pending key is written before publication to prevent
one sweep from asking repeatedly. `unreachable` means no observation was made,
not that the value is absent.

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

Delivery is at least once. Consumer idempotency belongs to the durable owner:
a database lock, persisted request/run identity, or explicit turn/adoption key.
`XAUTOCLAIM` recovery, DLQ handling, and approximate stream trimming do not
authorise a consumer to invent a result. The PO consumer additionally tracks
its in-flight ids so one process does not reclaim its own active dispatch.

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

1. A durable deploy Run is created before `DeployMessage` publication.
2. The deploy consumer records the dispatch boundary before GitHub Actions is
   no longer safely stoppable, then writes its typed terminal result.
3. The supervisor reads that typed result, creates QA work only with resolved
   repository criteria, and routes the typed QA outcome.
4. A terminal owner notification is persisted before it is published to PO.
   Recovery retries the owned notification record; it does not duplicate an
   already settled owner event.

## REST DTO registry

REST model fields, validators, and JSON aliases are authoritative only in the
sources named here. `services/api/src/schemas/` contains API-local response or
composition models where listed. In API-exposure cells, `schemas/...` and
`routers/...` paths are relative to `services/api/src/`.

### Project and repository surfaces

<a id="projectdto"></a>

| Surface / model family | Canonical source | API exposure / owner | Non-type invariant |
|---|---|---|---|
| Project create/update/status/teardown | `shared/contracts/dto/project.py` | `schemas/project.py`, `routers/projects.py` | initiating run ownership is required before worker-producing work; pre-ownership projects are readable but refused for that work |
| Repository create/update/role | `shared/contracts/dto/repository.py` | `schemas/repository.py`, `routers/repositories.py` | repository acceptance criteria and bot binding are durable sources for QA |
| Service modules and port roles | `shared/contracts/dto/project.py`, `shared/contracts/service_ports.py` | project and allocation routes | deploy URL selection uses the public service role, not an arbitrary allocation |
| Application status | `shared/contracts/dto/application.py` | `schemas/application.py`, `routers/applications.py` | application state is not a substitute for a typed deploy Run outcome |
| Server and SSH user/status | `shared/contracts/dto/server.py` | `schemas/server.py`, `routers/servers.py` | server operations use the resolved caller principal |
| Service deployment result | `shared/contracts/dto/deployment.py` | `schemas/service_deployment.py`, `routers/service_deployments.py` | deployment rows identify an owned application target |
| User create/update | `shared/contracts/dto/user.py` | `schemas/user.py`, `routers/users.py` | an API caller cannot substitute another bearer subject |

### Story, task, and run surfaces

<a id="taskdto"></a>

| Surface / model family | Canonical source | API exposure / owner | Non-type invariant |
|---|---|---|---|
| Story create/update/status | `shared/contracts/dto/story.py` | `schemas/story.py`, `routers/stories.py` | owner notifications and QA handoff are durable story lifecycle state |
| Task create/update/event/status | `shared/contracts/dto/task.py` | `schemas/task.py`, `routers/tasks.py` | scheduler dispatches only durable eligible task state |
| Task action requests | `services/api/src/schemas/actions.py` | `routers/_task_actions.py` | actions use admission and do not bypass paid-run ownership |
| Run create/type/status | `shared/contracts/dto/run.py` | `schemas/run.py`, `routers/runs.py` | terminal transitions are guarded by the Run owner and lock |
| Typed run results | `shared/contracts/dto/run_result.py` | `schemas/run.py`, deploy/QA consumers | only the owning terminal writer may set its typed result; readers reject a mismatched or untyped shape |
| Engineering attempt ledger input | `shared/contracts/dto/engineering_attempt.py` | `schemas/run.py`, `routers/runs.py` | terminal ledger fact is idempotent by engineering Run |
| Owner notification | `shared/contracts/dto/owner_notification.py` | `schemas/story.py`, `routers/stories.py` | persist notification obligation before PO publish; retry from that record |

<a id="rundto"></a>

### Operations and policy surfaces

| Surface / model family | Canonical source | API exposure / owner | Non-type invariant |
|---|---|---|---|
| Temporary access | `shared/contracts/dto/temporary_access.py` | `schemas/temporary_access.py`, `routers/temporary_access.py` | grants, revocation, and observations are durable lifecycle facts |
| Deploy dispatch | `shared/contracts/dto/deploy_dispatch.py` | `routers/runs.py` | dispatch claim/withdrawal is ordered under the Run lock |
| QA handoff | `shared/contracts/dto/qa_handoff.py` | `routers/stories.py` | the plan binds a QA attempt to its deploy provenance |
| QA SSH grant | `shared/contracts/dto/qa_ssh_grant.py` | `routers/runs.py` | grant only the run-scoped restricted target access |
| Engineering consumer drain | `shared/contracts/dto/engineering_consumer.py` | `routers/engineering_consumer.py` | a drain is durable and audited; a recreated consumer honours it |
| Work admission | `shared/contracts/dto/work_admission.py` | `routers/work_admission.py` | command identity does not reopen terminal work |
| Budget policy | `shared/contracts/dto/engineering_budget_policy.py` | `routers/engineering_budget_policies.py` | integer micro-USD and optimistic versioning |
| Executor decision/diagnostics | `shared/contracts/dto/executor_decision.py`, `dto/executor_diagnostics.py` | admission/overview routes | persisted decision wins over later configuration |
| Admin overview | `shared/contracts/dto/admin_overview.py` | `routers/admin_overview.py` | unavailable observations remain unavailable |
| Incidents, analytics, brainstorms | `dto/incident.py`, `dto/analytics.py`, `dto/brainstorm.py` | corresponding schema and router modules | their status vocabularies are source-owned |
| Agent config, API key, system config | `dto/agent_config.py`, `dto/api_key.py` and `schemas/system_config.py` | corresponding API routers | API-local system-config payloads are not shared DTOs |
| Telegram binding | `shared/contracts/dto/telegram.py` | `routers/projects.py` | token binding is fail-closed and stores the verified bot identity |
| API analytics and RAG payloads | `services/api/src/schemas/analytics.py`, `schemas/rag.py` | `routers/analytics.py`, `routers/rag.py` | these API-local models have no shared duplicate |
| Promo-code and port allocation payloads | `services/api/src/schemas/promo_code.py`, `schemas/port_allocation.py` | promo-code and allocation routes | route ownership determines admission and visibility |

### Shared DTO foundations

`shared/contracts/dto/base.py` supplies common API DTO foundations. The API also
has local schemas for analytics, brainstorming, API keys, ports, RAG, promo
codes, system configuration, and LK interactions. Their canonical definitions
are the corresponding `services/api/src/schemas/*.py` modules unless the table
above names a shared contract import.

## Queue message registry

<a id="scaffoldmessage"></a>

| Message / result family | Canonical source | Producers | Consumers | Delivery and ownership rule |
|---|---|---|---|---|
| `ScaffoldMessage` | `queues/scaffold.py` | scheduler | scaffolder | scaffold durable state is claimed before work and settled through typed result paths |
| `ArchitectMessage` | `queues/architect.py` | PO/API and scheduler | architect consumer | story identity, not conversational state, drives decomposition |
| `EngineeringMessage`, `EngineeringResult` | `queues/engineering.py` | scheduler | engineering consumer | task id names the immutable paid Run decision; initiating run id fences worker ownership |
| `DeployMessage`, `DeployResult`, triggers/actions/outcomes | `queues/deploy.py` | scheduler/API | deploy consumer | recipient rule is address xor reason; terminal result belongs to deploy Run owner |
| `QAMessage`, QA outcomes | `queues/qa.py` | supervisor/admin action | QA consumer | run id names the QA decision; criteria are resolved before publication |
| worker commands/responses | `queues/worker.py` | langgraph / worker-manager | worker-manager / langgraph | only lifecycle owner creates, deletes, or answers a worker command |
| developer input/output | `queues/developer_worker.py` | developer node / wrapper | wrapper / developer node | broker request id and single typed accepted output settle a leased turn |
| provisioning request/result | `queues/provisioner.py` | scheduler / infra-service | infra-service / scheduler and bot | result consumers use their own group semantics |
| environment observation | `queues/env_observation.py` | temporary-access sweep | infra-service | result is durable Redis observation, not inferred deploy state |
| PO input/response/proactive | `queues/po.py` | bot/system/PO | PO/bot | flat codec and recipient validation apply before consumption |
| progress event | `events.py` | services | bot | progress does not authorise state transition |

## Lifecycle and security invariants

### Typed `Run.result` and terminal ownership

`shared/contracts/dto/run_result.py` defines typed deploy and QA result families.
The terminal consumer that owns a Run writes its result while performing the
terminal transition. Supervisors route only a matching typed result for that
Run type; a missing, malformed, or foreign shape is an infrastructure/lifecycle
problem, never a successful or engineering-fix verdict.

### Deploy dispatch, withdrawal, and deadlines

`shared/contracts/dto/deploy_dispatch.py` and `services/api/src/routers/runs.py`
own the dispatch record. Claiming the crossing to GitHub Actions and withdrawing
an unclaimed dispatch are ordered under the Run lock. Before the crossing a
stop/revoke may withdraw; after it the recorded lease/deadline drives recovery
and cancellation handling. A revoke that must be the final deploy writer fences
older active deploys instead of allowing an earlier run to restore the value.

### Temporary access

Canonical contracts: `dto/temporary_access.py`, `dto/qa_ssh_grant.py`,
`queues/env_observation.py`, and `shared/contracts/env_overrides.py`.

Persist the grant/revoke obligation before publishing the deploy or observation
that advances it. A grant is scoped to the named run and restricted capability;
the target never receives a general SSH identity. Revocation remains recoverable
until a durable observation proves the running service no longer has the value.
An unreachable observation proves neither presence nor absence.

### QA handoff and restricted access

`dto/qa_handoff.py` binds QA to deploy provenance. Health-only criteria run over
HTTP without an executor. Other criteria use the central ephemeral QA executor
through worker-manager; API-agent fallback is only after that executor path
fails. The executor receives a run-scoped restricted capability, no target SSH
credential, and egress only through the assigned proxy. Failure to establish
that boundary is a typed infrastructure outcome, not a product verdict.

QA parses criteria before it resolves exploratory-only resources. Deterministic
probe inability, unavailable target runtime, bot liveness failures, access
denials, and product check failures retain distinct typed classifications.
Only a typed failed product result is eligible for an engineering fix loop.

### Terminal owner notification

`dto/owner_notification.py` and `shared/contracts/queues/po.py` define the
handoff. Persist the owed owner notification before PO publication. Recovery
publishes the durable obligation once; it does not turn a duplicate queue event
into a second owner notification. A deployed address is included only when the
typed lifecycle state authorises it.

### Engineering rollout, drain, and adoption

`dto/engineering_consumer.py`, `shared/contracts/worker_turn.py`, and the
engineering consumer own rollout continuity. A drain is stored and audited
before a consumer stops claiming work. Replacement consumers reclaim/adopt the
same compatible PEL turn rather than publish a duplicate prompt. A terminal
settlement tears down unconsumed turns only through their ownership fence.

Operator inventory distinguishes Docker observation, active turn lease, story
binding, and waiting attempt. An unreadable source is unavailable, not absent;
a live but unowned container must not appear healthy.

### Worker ownership, teardown, and removal evidence

`queues/worker.py`, `shared/contracts/worker_evidence.py`, and
`shared/contracts/worker_control_plane.py` are canonical.

Every dynamic worker carries non-empty project, initiating-run, and attempt
ownership. The create path records ownership before a container can exit. A
delete path captures durable removal evidence before removing the container and
before deleting worker metadata; if durable attribution cannot be written, the
last metadata name is retained rather than silently losing the worker from run
evidence. Cleanup selects only the owning run's labels, verifies removal, is
idempotent, and refuses an unscoped or neighbour-owned resource.

### Provisioning and environment observation

Infra-service owns provider observation/client code; policy decisions stay in
`shared/provisioning_policy.py`. Provisioning success is not inferred from a
request publication. Environment observation reads the running target through
the infra boundary and reports its typed outcome; it does not read a repository
file or assume a dispatch changed the deployed service.

## Source map

| Area | Source of truth |
|---|---|
| shared REST DTOs | `shared/contracts/dto/` |
| queue messages and results | `shared/contracts/queues/` |
| shared Redis topology and client semantics | `shared/queues.py`, `shared/redis/client.py` |
| shared run, recipient, worker, and env invariants | `shared/contracts/` |
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
5. Preserve compatibility only when current code accepts or rejects a real
   persisted row or wire payload; state the current rule, not its chronology.
