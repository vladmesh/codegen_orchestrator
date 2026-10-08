# REST story, task, run, and policy surfaces

Canonical REST DTO registry for project/repository, Story/Task/Run, policy, and shared DTO foundations.

Back to the [contracts index](../CONTRACTS.md).

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
| Application status | `shared/contracts/dto/application.py` | `schemas/application.py`, `routers/applications.py` | application state is not a substitute for a typed deploy Run outcome; `not_deployed` releases only that application's runtime port allocations in the same transaction, while `stopped` retains them; `monitoring_enabled` is a separate admin switch (`POST /applications/{id}/monitoring`) that only stops health probes and alerts, never a status change |
| Server and SSH user/status | `shared/contracts/dto/server.py` | `schemas/server.py`, `routers/servers.py` | server operations use the resolved caller principal |
| Service deployment result | `shared/contracts/dto/deployment.py` | `schemas/service_deployment.py`, `routers/service_deployments.py` | deployment rows identify an owned application target |
| User create/update | `shared/contracts/dto/user.py` | `schemas/user.py`, `routers/users.py` | an API caller cannot substitute another bearer subject |
| QA probe library | `shared/contracts/dto/qa_probe_library.py` | `routers/projects/qa_probes.py` | `GET /projects/{id}/qa-probes` is internal or admin; `POST /projects/{id}/qa-probes/from-run` is the QA runtime only and reads the probes off the settled passed Run, never the request |

### Story, task, and run surfaces

<a id="taskdto"></a>

| Surface / model family | Canonical source | API exposure / owner | Non-type invariant |
|---|---|---|---|
| Story create/update/status | `shared/contracts/dto/story.py` | `schemas/story.py`, `routers/stories.py`, `routers/_story_helpers.py`, `routers/_story_actions.py` | status, `waiting_on` and `status_entered_at` are written only by a transition, together on one locked row; `StoryUpdate` refuses all three; App-authenticated generated-product evidence, owner notifications and QA handoff are durable story lifecycle state; `unverified_decisions` is append-only |
| Task create/update/event/status | `shared/contracts/dto/task.py` | `schemas/task.py`, `routers/tasks.py` | scheduler dispatches only durable eligible task state |
| Product Brief and requirement coverage | `shared/contracts/dto/product_brief.py` | `routers/product_briefs.py` | confirmed content is immutable; one live planning attempt; one idempotent admission releases that attempt's tasks |
| Task action requests | `services/api/src/schemas/actions.py` | `routers/_task_actions.py` | actions use admission and do not bypass paid-run ownership |
| Run create/type/status | `shared/contracts/dto/run.py` | `schemas/run.py`, `routers/runs.py` | terminal transitions are guarded by the Run owner and lock |
| Typed run results | `shared/contracts/dto/run_result.py` | `schemas/run.py`, deploy/QA consumers | only the owning terminal writer may set its typed result; readers reject a mismatched or untyped shape |
| Engineering and QA attempt ledger input | `shared/contracts/dto/engineering_attempt.py` | `schemas/run.py`, `routers/runs.py` | terminal ledger fact is idempotent by Run |
| Owner notification | `shared/contracts/dto/owner_notification.py` | `schemas/story.py`, `routers/stories.py` | persist notification obligation before PO publish; retry from that record |
| Lifecycle-wait moves | `shared/contracts/dto/lifecycle_wait.py` | `routers/_resource_wait_actions.py`, `routers/_story_actions.py` | the move and its owed owner notice on the deciding Run commit in one transaction; the API mints the record's facts |

**Story lifecycle ownership.** A Story's status is written in exactly two places,
both in `services/api`: `_do_transition` in `routers/_story_helpers.py` for a
single hop, and `_apply_chain` in `routers/_story_actions.py` for a composite
move declared in `COMPOSITE_CHAINS`. Both read the row through
`_get_story_for_update` and validate every hop against `VALID_TRANSITIONS`
before applying any, so a composite is all-or-nothing on one locked row inside
one transaction rather than a client-side sequence a crash can leave halfway.
`COMPOSITE_CHAINS` has one entry today — `retry-after-ci-failure`, the
`failed → reopened → in_progress` move exposed as
`POST /api/stories/{id}/retry-after-ci-failure` — and nothing outside that table
walks a Story through more than one status. Every other caller reports the event
that happened through a single-hop action; no path in `services/scheduler` or
`services/langgraph` issues two Story transitions for one story.

The locked infrastructure park,
`POST /api/stories/{id}/park-infrastructure-refusal`, moves a Story one hop but
its Task up to two (`todo → in_dev → waiting_human_review`) in the same
transaction, so no caller sequences task and story status for that park.
`POST /api/stories/{id}/park-waiting-user-secret` moves a Story one hop together
with the owed ask on its deploy Run (see the lifecycle-wait table above).

**A platform failure names itself on the story.** `POST /api/stories/{id}/fail` and
`/human-review` accept an optional `failure` (`StoryFailure`, `shared/contracts/dto/story_failure.py`:
`code`, `source`, redacted and bounded `detail`). The same transaction stores it as
`quarantine_reason` (`reason: story_failure`) and owes the owner (`story_failed` / `story_blocked`)
and administrators the cause. The scaffolder sends `scaffold_failed` for every story still waiting on
a failed scaffold (`created`, or `in_progress` with no task of its current cycle); the architect sends
`scaffold_failed` (fail) when the project carries `scaffold_error` and `scaffold_timeout` (park) when
its wait runs out. `GET /api/stories/{id}/diagnostics` (`StoryDiagnosticsRead`, project access) is
the read-only view of the causes: typed failure or other `quarantine_reason`, `scaffold_error`,
work-cycle task count, the last failed Runs and task stop events, and the newest error/warning Loki
lines naming the story or project — named fields only, redacted, at most
`STORY_DIAGNOSTIC_LOG_LIMIT`; an unreadable log store is `logs_unavailable`, never an error.

Empty engineering stops use `StoryFailureCode.NO_NEW_COMMIT` (`no_new_commit`, source
`engineering` or `scheduler`) on `/human-review`: taskless results, exhausted planned-task
retries and GitHub's specific no-commits PR refusal commit the reason, `waiting_on=human_review`
and both owed audiences together. No separate reason PATCH or immediate Redis publication is
required; the existing notification sweep delivers `story_blocked` with the bounded cause and
the explanation that nothing was produced and a person must decide the next move.

Failed-task supervision selects the required typed stop for each story before any exhausted
sibling can make a bare escalation. It reads each selected task's latest engineering result;
an unread sibling leaves that story selected. A partial handoff resumes only when the story's
status/waiting reason, exact task/attempt cause and owner/admin notification episode match the
empty-result stop. A settled exhausted sibling can supply that same proof. Unrelated human
review or a missing/mismatched notice is not proof; no repeated transition is globally allowed.

For `DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED`, the DevOps subgraph preserves the typed
resolver outcome and key-bearing error even when `deployment_result` is absent or null.
The deploy consumer commits a failed Run with bounded/redacted cause and performs no deploy
execution after resolver failure. The scheduler consumes that persisted result and sends
`StoryFailureCode.ENVIRONMENT_RESOLUTION_FAILED`, source `scheduler`, through `stop_story`
with action `fail`. The API commits the failed status, cause and owed owner/admin notices
together. Stop refusal propagates for retry; notification publication is recovered by the
existing owed-notice sweep. No preceding reason PATCH is required. Other deploy outcomes
retain their existing routes.

**A failed planning attempt is a story state.** `stories.planning` (`StoryPlanning`, on
`StoryRead.planning`, `shared/contracts/dto/story_planning.py`) is the one durable record that
planning is owed: every path that makes the architect owe a story a planning run writes it as
`retrying` with `next_attempt_at` in the same transaction as the state change. The architect reports
a failed attempt to `POST /api/stories/{id}/planning-outcome` (`StoryPlanningReport` →
`StoryRead`, internal or admin) with a `planning_failed` `StoryFailure` (source `architect`,
redacted detail naming the error class and, for `LLMChannelsExhausted`, every channel with its
class). A retriable failure within `supervisor.story_max_architect_retries` is `retrying` with
`failed_attempts` and `next_attempt_at` (60 s, doubling) and leaves the status alone. A failure past
the bound, or `retriable=false` (every channel failed with payment_required, unauthorized, forbidden,
quota_exhausted, missing_credential or binary_missing, or the chain cannot run), is `parked`: the
same transaction makes the `human-review` stop with the `StoryFailure` and both owed notices (a
`reopened` or `created` story passes through `in_progress`). A failure reported for a story outside
`created`/`in_progress`/`reopened` is a 409 and writes nothing. `POST /api/stories/{id}/retry-planning`
(internal or admin, optional `StoryPlanningRetryRequest`) is valid only for `waiting_human_review` with a
`planning_failed` stop, else 422: one transaction clears the stop, lands on `in_progress` and writes
`retrying` due now with `failed_attempts` 0, and nothing is published.
For an admitted all-returned brief with no Tasks in its attempt's release set, retry clears the admission
stamp under Brief-before-Story locks, so the next claim can plan the same confirmed order.
The shared returned-plan predicate excludes superseded-attempt Tasks. Other planning failures
retry without changing the Brief, including admitted reopens with original work.
The scheduler supervisor
(`supervise_stuck_stories`, one sequential loop in `scheduler-pipeline`) is the one publisher of an
`ArchitectMessage` for a `retrying` record, so an operator's re-run is queued within one cycle. The
Redis key `planning_retry_queued_key` (TTL `supervisor.story_retry_ttl`) is its throttle, never a
lock: it is checked first and set only after `XADD` returned, so a failed lookup or publish leaves
none and the next tick publishes again (a per-story failure is logged as
`story_planning_retry_publish_failed` and the tick goes on). The row wins over Redis. The architect
settles a job whose `retrying` record is not yet due (`architect_planning_not_due`), a claim that
finds a live rival, and a plan already in place.
A successful brief-backed plan records `planned` with its channels in the `admit` transaction
(`ProductBriefAdmissionCommand.channels` / `channel_failures`); a plan without a brief reports
`succeeded` to `planning-outcome`, retried briefly, and logs `architect_planning_outcome_unrecorded`
with the channels when the API stays unavailable.

*Accepted residuals (observer decisions on card codegen-orchestrator-1387).* A successful plan
without a Product Brief made while the API is unavailable for all three outcome writes keeps its
channels only in the `architect_planning_outcome_unrecorded` log event. An `XADD` that raises after
Redis wrote the entry sets no throttle, so the next tick may publish the same record again: a
brief-backed story's claim settles that duplicate, and for a story without a brief a second planning
run is possible in this Redis-fault corner only.

The state-age watchdog's ending, `POST /api/stories/{id}/expire-state-wait`
(`StateWaitExpiryCommand` → `StateWaitExpiryRead`, `shared/contracts/dto/state_wait.py`), moves a
Story one hop only if the locked rows still show the expected status and anchor
(`StateWaitExpiryCommand.mismatch`); reason, owed story record and transition commit together.
A mismatch is a typed `skipped` naming it, a repeat is `already_ended`, and neither writes anything.

The narrow exception is the locked infrastructure recovery transaction,
`POST /api/stories/{id}/retry-infrastructure-attempt`. It verifies the task and
story still carry the same exact pre-agent park, settles its refused Run fence
when one exists, records the legal task hops `waiting_human_review → backlog →
todo`, clears only that matching park, and restarts the story at `in_progress`
without changing the iteration. A matching completed audit returns the typed
`already_retried` no-op; stale evidence, a changed status, a non-infrastructure
park, or a mismatched Run returns a typed 409 and commits nothing.

Planning and infrastructure retry additionally select `stop_id` when an
unreleased `engineering_stop` exists. The native authenticated action releases
only that selected stop, audits its credential-derived actor, and performs the
existing transition in one transaction. A stale or missing selection returns
409; the admin buttons carry the stop shown with their reviewed target.

**`waiting_on` belongs to the transition, not to the caller.** `stories.waiting_on`
is a non-nullable typed `StoryWaitingOn` column (migration `c3f7a91d2b48`)
written only by `_land_on` in `routers/_story_helpers.py` — reached from story
creation, `_do_transition` and `_apply_chain` — on the same locked row and in the
same transaction as `status`, from the one `WAITING_ON_BY_STATUS` mapping in
`shared/contracts/dto/story.py`. That mapping is total over `StoryStatus`, so no
transition can leave a stale wait behind. `PATCH /api/stories/{id}` refuses
`status` and `waiting_on` alike — they are `TRANSITION_OWNED_STORY_FIELDS`, so
sending either is a 422 rather than a field silently dropped. `_land_on` also stamps
`stories.status_entered_at` (nullable timestamptz, migration `a4c6e8f0b2d5`, not backfilled) with
the landing time; it is read-only on `StoryDTO`/`StoryRead` and refused by `PATCH` the same way. `StoryDTO` and
`StoryRead` both declare `waiting_on` required with no default, so a response
without it is a broken response and not a story waiting for nothing, and
`GET /api/admin/overview` exposes it per story in the bounded `waiting_stories`
section (`WaitingStory`, at most `WAITING_STORY_LIMIT` rows, filtered in SQL on
`waiting_on != none` and read exactly as the transition wrote it).
`StoryWaitingOn.RESOURCES` is declared and unset: no Story status maps to it,
because work parks for resources at the *Task* level (`waiting_resources`) while
the Story stays `in_progress`.

**Generated-product evidence stays with the Story.**
`stories.generated_product_timeline` is the durable JSON record of the exact PR
and distinct `ci.yml` Runs the scheduler observed through its existing GitHub App
credential. A failed story-branch run is merged there before its CI-fix task is
created, the Story is retried, or exhausted attempts park it for human review;
the task retains the same run URL and identity, branch/head, conclusion, failed
jobs and steps, bounded redacted excerpt, and named unavailability. Repeated
polls and later default-branch publication observations merge positive facts by
run id without duplicates. Missed captures and detail-unavailability claims are
recomputed from the current merged observation rather than retained after the
fact becomes available.
A terminal publication failure also copies its evidence into `quarantine_reason`.
The Product Brief harness retains its existing Story reads as evidence schema
v17, and stand acceptance copies that artifact without GitHub access to the
generated-product organization or a read of the generated repository.

**Developer completion reports survive refusal.** The worker wrapper treats an
actual `REPORT.md` as authoritative and uses a completed result's content only
when no fuller report exists. If ordered commit verification refuses completion,
that report crosses the failed worker result and developer node into the existing
engineering-attempt `worker_report` task event; the refusal never becomes a
completed result.

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
| Engineering dispatch admission | `shared/contracts/dto/engineering_dispatch.py` | `engineering_dispatch_admission.py`, `routers/work_admission.py` | one decision per dispatch, one typed reason per refusal, and no repair performed by the decider |
| Budget policy | `shared/contracts/dto/engineering_budget_policy.py` | `routers/engineering_budget_policies.py` | integer micro-USD and optimistic versioning |
| Executor decision/diagnostics | `shared/contracts/dto/executor_decision.py`, `dto/executor_diagnostics.py` | admission/overview routes | persisted decision wins over later configuration |
| Admin overview | `shared/contracts/dto/admin_overview.py` | `routers/admin_overview.py` | unavailable observations remain unavailable |
| Incidents, analytics, brainstorms | `dto/incident.py`, `dto/analytics.py`, `dto/brainstorm.py` | corresponding schema and router modules | their status vocabularies are source-owned |
| System config | `services/api/src/schemas/system_config.py` | corresponding API routers | API-local system-config payloads are not shared DTOs |
| Telegram binding | `shared/contracts/dto/telegram.py` | `routers/projects.py` | token binding is fail-closed and stores the verified bot identity |
| API analytics payloads | `services/api/src/schemas/analytics.py` | `routers/analytics.py` | these API-local models have no shared duplicate |
| Promo-code and port allocation payloads | `services/api/src/schemas/promo_code.py`, `schemas/port_allocation.py` | promo-code and allocation routes | route ownership determines admission and visibility |

### Shared DTO foundations

Internal/admin publication and stop actions are defined in
[Commit publication and engineering stop](commit-publication-and-stop.md).
`StoryRead.engineering_stop` is server-owned; recovery request identity is only
Story, attempt, exact SHA, deliberate adoption and the current stop. Run/Task/Project
generic patches cannot forge or clear the preserved-publication keys.

`shared/contracts/dto/base.py` supplies common API DTO foundations. The API also
has local schemas for analytics, brainstorming, API keys, ports, promo
codes, system configuration, and LK interactions. Their canonical definitions
are the corresponding `services/api/src/schemas/*.py` modules unless the table
above names a shared contract import.

## Mechanical install tasks

TaskCreate type=install requires CatalogInstall plus story/repository ownership;
TaskRead and TaskDTO carry identical install and API-owned install_operation data.
TaskUpdate cannot rewrite that ownership or supply execution fields. Generic
start/complete/retry/resume are refused; DELETE preserves cancellation, releasing
queued operations while running writers still owe owned settlement. Internal/admin
catalog-install commands admit/claim/checkpoint/publish/refuse without a Run.
Bearer-admin recovery selects the current operation and matching stop/cause. See
[the install contract](kit-template-and-qa.md#installing-a-kit-package-into-a-generated-product).
