# Work admission and engineering attempts

Canonical accounting, admission, budget, and executor-selection rules for engineering work.

Back to the [contracts index](../CONTRACTS.md).

Committed Story stops and preserved unpublished commits precede ordinary bounded
paid retry, including taskless deploy fixes and conflict repairs. They cannot be
overridden by an actor string or automatic admission repair. See the canonical
[publication/stop authority and recovery contract](commit-publication-and-stop.md).

## Engineering attempt ledger

Canonical model: `shared/contracts/dto/engineering_attempt.py`.

`engineering_attempt_ledger` records one terminal coding-agent attempt under
the stable `engineering-run:{run_id}` identity. The terminal Run writer holds
the Run lock while it writes the ledger, so redelivery retains the first fact.
Engineering Runs always write a row. QA Runs reserve on paid admission and
send `qa_accounting` on terminal update: an executor start writes `role=qa`
and settles reported cost or retains an unknown-final hold; no start releases
the hold without spend. A missing QA fact also releases the hold and logs a
warning. An in-flight QA Run admitted before reservations existed has no hold
or spend to settle. The QA consumer counts every published executor create in
one Run; it reports the sum only when every start has typed provider facts,
and otherwise reports unknown cost.
Money is integer micro-USD, never float. Unknown cost is null, not zero; a
provider-reported cost must name both a provider and an amount. A project
deletion detaches relationship ids from accounting history without deleting the
accounting fact.

Provider evidence is parsed only by the worker-wrapper paths that own it. Claude
may contribute its documented terminal result facts; Factory contributes a
single valid result document without money; Codex stdout/stderr is not a usage
or cost source. The ledger validates internally consistent evidence before it
becomes an immutable terminal fact.

## Work admission and budgets

| Surface | Canonical source | API owner |
|---|---|---|
| paid-run command and outcomes | `shared/contracts/dto/work_admission.py` | `services/api/src/routers/work_admission.py` |
| engineering dispatch admission | `shared/contracts/dto/engineering_dispatch.py` | `services/api/src/engineering_dispatch_admission.py` |
| engineering execution boundary and infrastructure recovery | `shared/contracts/dto/engineering_execution.py` | worker-manager, engineering consumer, scheduler supervisor, `routers/_story_actions.py` |
| Product Brief coverage admission | `shared/contracts/dto/product_brief.py` | `services/api/src/routers/product_briefs.py` |
| per-user engineering budget policy | `shared/contracts/dto/engineering_budget_policy.py` | `services/api/src/routers/engineering_budget_policies.py` |
| executor decision snapshot | `shared/contracts/dto/executor_decision.py` | `services/api/src/work_admission.py` |
| executor diagnostics snapshot | `shared/contracts/dto/executor_diagnostics.py` | `services/api/src/executor_diagnostics.py` |

`POST /work-admission/engineering-dispatches` is the one admission point for
paid engineering dispatch: `services/api/src/engineering_dispatch_admission.py`
takes the task row (and, for a story task, the story row) for update, evaluates
the dispatchable status, the Product Brief coverage boundary, the
internal-project skip, the blocker, the project scaffold and `workspace_ready`,
the story lifecycle and the prior-attempt fence, and ends by calling
`start_paid_run` — it wraps the paid gate rather than standing beside it.
Every refusal carries one `EngineeringDispatchRefusal` value, so "story busy" is
distinguishable from "workspace not ready" and from "budget denied" without
parsing a log line. A prior attempt yields a named `EngineeringDispatchRepair`
that the caller executes; the decider performs no transition of its own. The
scheduler's `dispatch_todo_tasks` selects candidates, asks once per task, and
acts on the answer, and the admin route `POST /api/tasks/{id}/spawn-worker` asks
the same question before it publishes anything, behind an audited override
naming `task_not_dispatchable` and `live_attempt_in_flight` and nothing else.
Those two, plus the deploy supervisor's code-fix handoff — which dispatches no
Task and so has no admission to pass — are the only publishers of an
`EngineeringMessage`.

An operator spawn may walk past a condition only by naming it: a command carries
`overrides`, a list of `EngineeringDispatchRefusal` values restricted to
`OVERRIDABLE_REFUSALS`, the decision returns the ones it applied in `overridden`,
and the attempt records them in `run_metadata["admission_overrides"]`. The paid
gate and the project conditions are never overridable.

Two invariants hold the one-point guarantee up, and both are enforced in the API.

**One lock ladder, and every condition row on it.** `LOCK_LADDER` in
`engineering_dispatch_admission.py` declares the order once: Task rows — the
candidate, its blocker and its story's roster — in ascending task id, then the
Story, then the Project, then the engineering Run rows of those tasks, then the
paid-work control rows `start_paid_run` takes. Every row a condition reads is
taken through it with a locking reader (`get_task_for_update`,
`_get_story_for_update`, `load_locked_project`, `SELECT ... FOR UPDATE`).
Ascending task id is what keeps a reciprocal `blocked_by` from deadlocking. An
unlocked query is permitted only to learn *which* row to lock next, must be
column-only, and never supplies a value a condition uses. Its corollary binds
callers: no caller may materialise a subject row before calling admission,
because SQLAlchemy's identity map would then serve the locking read that stale
entity; a route that must know something first asks a column-only question, as
`spawn-worker` does. A story roster can still grow by INSERT, which no row lock
fences, so the roster is re-read under the story lock and a member the decision
does not hold ends the tick with `story_roster_changed`.

**One entry to paid engineering work on a Task.** A paid engineering run bound to
an existing Task row is created only by the admission point.
`POST /work-admission/paid-runs` refuses a `type=engineering` command whose
`task_id` names an existing Task with HTTP 409 and the typed code
`engineering_task_dispatch_requires_admission`. It remains the paid gate for
everything else, including the deploy supervisor's code-fix handoff, which names
no Task row. A non-null `task_id` that names no Task is refused before Run
creation with HTTP 422 and `engineering_task_not_found`; `runs.task_id` remains
the foreign-key backstop rather than an untyped database failure.

The paid-run command locks its controls, evaluates count admission and (for
engineering) money admission, then creates the queued Run in one transaction.
It records an audit decision. A queued or running replay returns the persisted
Run; a terminal identity is not reopened. A publish failure has an unknown
broker outcome, so the committed Run and reservation remain recoverable rather
than being cancelled speculatively.

`POST /work-admission/engineering-dispatches/start` accepts
`EngineeringAttemptStartCommand` from an internal service or administrator.
The command names only Task and admitted Run; actor text grants no authority.
The API uses dispatch admission's Task-roster, Story, Project, Run lock ladder
and re-reads the repair admission, cycle, PR, iteration and settlement ledger.
Only the same current live attempt can start; repeated starts return `reused`.
A settled attempt returns `settled`, a replaced attempt/cycle/PR returns `stale`,
and a failed terminal attempt returns `terminal_pending`, all without writes.
TODO terminal work remains discoverable by dispatch's scoped outcome recovery.
No response restores TODO from an old ledger or changes a newer live attempt.
Matching completed-Run recovery commits the native completion hops together
under this fence and returns `completed`, without replaying a generic start.
This command creates no Run, reservation, publication or notice.

Conflict dispatch publication, its repeated start request and live/prior-attempt
recovery use this command. Generic Task `start` and transitions to `in_dev`
refuse admitted conflict Tasks. Proven infrastructure/resource terminal evidence
returns `priority_pending` and restores only matching IN_DEV discovery under
the same fence, then defers to the existing native stuck priority routing.
Operator spawn retains admission locks and commits its start before publication;
conflict spawn cannot override a live attempt or revive settled/replaced work.
Unavailable or stale scoped responses never fall back to a Task-only start.

Internal/admin callers can read the immutable admission fact at
`GET /api/work-admission/paid-runs/{run_id}/admission` and the corresponding
reservation outcome at `GET /api/engineering-budget-policies/admissions/{attempt_id}`.
Neither endpoint retries or mutates admission. The latter reports the real
stored state: an unlimited or disabled policy has no held reservation, while an
enforced terminal attempt may be released, settled, or conservatively
`unknown_final` when no provider cost exists.

Admission writes an immutable `executor_decision` before a billable side effect.
QA admission skips model diagnostics only when its typed `qa_handoff` matches the
Run, Project and Story and contains exclusively HTTP GET criteria, parsed by the
same rule as the QA consumer. Count controls, budget admission and the persisted
executor decision still apply; absent, invalid or exploratory handoffs retain diagnostics.
Consumers load that decision by the engineering task id or QA run id; they do
not select an executor from mutable project or process configuration. A malformed
control or diagnostic is fail-closed. An administrator may confirm only a
specific unexpired `unknown` diagnostics snapshot; an internal service cannot
make that confirmation.

The diagnostics snapshot is schema `v2` under `executor:diagnostics:v2` with a
90-second TTL; a v1 or otherwise invalid value is typed `unknown`. An enabled
host-session diagnostic requires exactly one `ExecutorProfileObservation`, and
its reason code and availability are derived from the observation's closed
`condition` (`healthy` → `ready`/available, `refresh_expiring` → degraded,
`refresh_expired`/`refresh_missing`/`logged_out`/`unusable` → unavailable,
`unverifiable`/`read_contended` → unknown); only a healthy or expiring profile
defers to `inventory_unreconciled`. Access/session expiry is reported but stays
renewable while refresh material exists; only a locally proved refresh-credential
expiry drives `refresh_expiring` (24 hours) and `refresh_expired`. The Codex
reader, shared by worker creation and diagnostics, observes in a fixed order: a
stable `auth.json` read that joins the wrapper's `.codegen-codex.lock` (only a
held shared lock on the stable lock inode is authoritative; a missing lock never
proves no writer, and a torn read otherwise is `read_contended`, never logged
out), then the pinned `AuthDotJson`/`TokenData` shape, including both
`AgentIdentityStorage` variants, parsed no more permissively than `serde_json`
(a file the CLI cannot load, or a struct stored as an array, is `unusable`).
Every reader parses JSON through one total boundary, `host_profile.load_json`:
standard JSON only (never `NaN`, `Infinity` or `-Infinity`), at most 1 MiB of
strict UTF-8 and nesting at most 127 for every reader, plus, for Codex auth.json
and JWT claims only, no duplicate keys, lone surrogates or number the pinned
serde_json 1.0.149 reports as `NumberOutOfRange`; it returns one failure result
instead of raising. Then comes the authoritative `auth_mode` (anything but the ChatGPT
subscription mode is `unusable`), and only then ChatGPT token material. The
worker wrapper creates the lock inode at startup and the login recipe creates it
before logging in. Timestamps are timezone-aware, each time fact names its
executor-specific source, and an access-token expiry can never be stored as a
refresh expiry. Worker-manager's `ExecutorDiagnostics` publisher alone writes the
snapshot and reconciles `ExecutorProfileAlertEpisode` records, whose delivery
outcomes are the `AdminDeliveryStatus` values. One episode spans an executor's
whole unhealthy stretch: later alertable observations update its condition and
refresh expiry without reopening delivery, `read_contended` neither opens nor
resolves it, and only a healthy observation deletes it.

`EngineeringExecutionEvidence` is the authoritative boundary for whether an
engineering agent started. It is exactly either `agent_started` with no refusal,
or `pre_agent_refused` with one `EngineeringInfrastructureRefusal`. The evidence
travels in worker status, `AttemptTurnMetadata`, and `EngineeringRunResult` on
the same Run. Missing, malformed, legacy, or contradictory evidence is not a
free attempt and must follow the ordinary failure path; consumers never infer
this fact from prose, tokens, elapsed time, or container presence.

For a valid pre-agent refusal, one exact `EngineeringInfrastructurePark` is
stored under `engineering_infrastructure` in task `failure_metadata` and, for a
story-bound task, story `quarantine_reason`. Admission owns
`executor_unavailable` and `executor_confirmation_required`; the liveness
supervisor owns post-handoff worker creation refusals. Both preserve
`current_iteration` and retry accounting.

One API function, `apply_infrastructure_park` (`services/api/src/infrastructure_park.py`),
writes every park on rows its caller already holds locked (Task, then Story), and
never commits. It applies the legal audited task hops from `todo`, `in_dev`, or
`failed`, writes both evidence copies, moves the story to `waiting_human_review`,
and owes both notice audiences on the story's terminal-notification record. Its
dispositions are `parked`, the repeat no-op `already_parked`, and
`ineligible_story` for a terminal or otherwise transition-ineligible story, which
changes neither row. Different evidence, a half-parked row, a row already in
human review, or a non-parkable task status is a typed 409.

Admission is the sole linearization point for a paid pre-agent refusal: in the
same transaction that writes the paid-work `WorkAdmissionAudit`, and under the
task and story admission locks, it parks with that audit's attempt id, reason and
message and returns the result as `EngineeringDispatchRead.infrastructure_park`.
A lost answer therefore leaves a task that is no longer `todo`, and the scheduler
never parks this refusal again. A standalone task is parked on the task alone.

Admission is also the one place a failed ensure-workspace becomes a park. When the
locked project row carries `scaffold_error` without `workspace_ready` (the
scaffolder records it for a failed clone/setup and for an exception in the ensure
job), rung 3 refuses with `EngineeringDispatchRefusal.WORKSPACE_ENSURE_FAILED` and,
for a parkable task whose story is not already in human review, parks it as
`workspace_ensure_failed` with a fresh `ws-` attempt id, a detail naming the
redacted error, and a `WorkAdmissionAudit` of subject `workspace_ensure` in the same
transaction. That audit is the only proof the park endpoint accepts for this
refusal; it proves no other refusal. `retry-infrastructure-attempt` for this
refusal also removes `scaffold_error`, and nothing else, from `project.config`
under the project lock, so ensure runs again; a new failure parks again.

The liveness supervisor parks a Run-backed refusal through the internal/admin
`POST /api/stories/{id}/park-infrastructure-refusal`
(`EngineeringInfrastructureParkCommand` → `EngineeringInfrastructureParkRead`).
The command is never authority by itself: the locked refused Run must match task,
story, typed refusal and the detail derived from it; without a Run, the unique
committed paid-work audit must match task, story, current iteration, attempt id,
typed reason and message. A missing proof is `refusal_evidence_missing`, more than
one audit is `refusal_evidence_ambiguous`, any mismatch is `stale_attempt_fence`,
and none of them mutates anything. Admission also refuses a task or story that
already carries a park with `infrastructure_parked` before any attempt id is minted.

`OwnerNotification.delivered_at` is set by the seam in the same write that marks the owner
audience `delivered`, and is `None` before that and on records delivered before the field
existed. The `waiting_user_secret` age bound continues to read delivery acceptance,
never `owed_at` or the independent `told_at`.

`POSystemEvent.owner_notice` identifies a durable obligation by source (`run` or `story`),
source id and `owed_at`, JSON-encoded in one flat Redis field. Best-effort events carry none.
`DELIVERED` still means acceptance by `po:input`; the separate `told_state` is absent until
PO records `told`, `suppressed` or `closed`, with the corresponding time and decision details.
The PO consumer checks that exact record at its single proactive publish point and writes
`told` only after publication. A failed write is logged without undoing the publication; an
unreadable publication decision leaves the input event pending.

Internal/admin `GET /api/stories/{id}/owner-notifications` reads both record homes, and
`POST /api/stories/{id}/owner-notifications/settlement` compares the source and `owed_at`
under row locks. A stale identity is 409. Suppression requires the latest delivered,
unsettled notice, a reason and `suppressed_by=po|user|admin`; a user-secret request is refused.
The PO tool exposes only `po|user`, verifies project ownership and refuses a latest best-effort
event, remembered by the consumer per chat/story. Suppression copies story, event, text,
reason and decider to admins; a failed immediate copy is owed to the existing admin audience.

`GET /api/stories/owner-notifications/deferred?project_id=...` lists suppressed notices without
an age cutoff. The snapshot reads this for the user's owned projects. PO resolves the oldest
deferred notice of a story as `told` after telling it in a user turn, or as `closed` on an
explicit user/admin drop request with a reason. Delivery writes preserve concurrent PO
settlement. Replacement retains suppressed obligations in the existing record's `deferred`
list, addressable by their original `owed_at` with explicit `resolve_deferred=true`, and carries
any pending admin copy forward. A publication write cannot settle a replaced record.
No new table or delivery-state value is introduced; old records remain unsettled, not deferred.

`OwnerNotification` carries an optional administrator audience (`admin_text`,
`admin_state`, `admin_attempts`, `admin_detail`) settled independently of the
owner through the same record, selection and bounded retries. Released records
have no such fields and are read as owing administrators nothing, so their owner
semantics are unchanged; no migration is needed because the record is JSON. The
owner audience is voided when its terminal status is gone; the administrator
audience describes a committed event and is delivered regardless.

The administrator audience settles on `shared.notifications.deliver_to_admins`,
whose `AdminDeliveryResult` carries the configured and successful recipient
counts, never on `notify_admins` not raising (`send_telegram_message` returns
`False` for rate limits, non-200 answers and timeouts; `notify_admins` keeps
returning only the success count for best-effort alerts). No configured
administrator settles `unaddressable` with the counts as `admin_detail`; all
configured recipients accepted settles `delivered`; zero or partial success, or
a raised users-API failure, spends one bounded attempt and stays `owed`, then
`abandoned` with the detail. A settled audience is never sent again; before
settlement delivery is at-least-once, because Telegram has no idempotency key,
so a retry after partial success resends to administrators already reached.

Attempts on one record are spaced by `OwnerNotification.last_attempt_at`, not by the order
the dispatcher calls routing and the recovery sweep. Every delivery first asks the internal
`POST /api/runs/{id}/owner-notification/attempt` or `POST /api/stories/{id}/owner-notification/attempt`
(`OwnerNotificationAttemptClaim`). Under the row lock the API grants it only while some audience
is `owed` and the last attempt is at least `OWNER_NOTIFICATION_ATTEMPT_INTERVAL` (60 s) old, and
stamps `last_attempt_at` in the same write; a refusal changes nothing and the caller publishes
nothing (`not_due`, or `skipped` when the record is settled). One stamp spaces both audiences,
because one granted visit serves both. A missing stamp — every record written before it existed —
means never attempted. A voided record keeps its stamp and spends no attempt; an ending owed
again is a fresh record with a new `owed_at` and no stamp. The run `PATCH` and the story
owner-notification `PATCH` answer 409 `owner_notification_attempt_superseded` to a write of the
same obligation (same `owed_at`) carrying an older or missing stamp than the stored one, so a
visit that outlived its claim cannot overwrite what a newer one settled. They answer the same 409
to a write naming an older obligation (an earlier `owed_at`) than the stored one: a later notice
on the same Run replaced it, and the visit to the replaced record may not write it back.

The non-terminal lifecycle notices are owed records too, written by the API in the transaction of
the move they announce (`shared/contracts/dto/lifecycle_wait.py`), never published directly:

| Move (internal/admin) | State change | Record on | True while |
|---|---|---|---|
| `POST /api/tasks/{id}/park-waiting-resources` | wait facts in `failure_metadata`, task → `waiting_resources` | the refused engineering Run (`run_id`) | task `waiting_resources`, story in its status at the park |
| `POST /api/tasks/{id}/resume-from-resource-wait` | task `waiting_resources → backlog → todo` | the task's latest engineering Run, replacing the wait's record | task `todo`/`in_dev`, story in its status at the resume |
| `POST /api/stories/{id}/park-waiting-user-secret` | story `deploying → waiting_user_secret` | the deploy Run that reported the missing secrets (`run_id`) | story `waiting_user_secret` |

The caller sends only the words (`event`/`text`); the API mints the record's facts from the locked
rows, so an illegal hop or a Run that is not the task's (`stale_attempt_fence`) writes neither
the move nor the record. A park is announced (`task_waiting_resources`, or
`task_waiting_infrastructure` for an unprovisioned host) only when it starts a wait — the locked
task carries no `resource_wait_started_at` yet — so a wait spanning several refused attempts is
announced once. A repeat answers `already_waiting` and a resume of a task no longer waiting
answers `not_waiting`, writing nothing. The secret ask keeps an ask the Run already carries; its
`delivered_at` stays the state-age anchor. A task-level record names the task statuses it is true
in (`OwnerNotification.expected_task_statuses`); delivery voids it, publishing nothing and
spending no attempt, when the task has left them. Records without that field keep exactly the
story check. The truth check is read again after the recipient is resolved, as the last reads
before the `XADD`; a move committing between those reads and Redis accepting the entry is not
seen, so no ordering is promised between two notices about one task. `park-waiting-user-secret`
is the only API route that lands a Story in `waiting_user_secret`; the single-hop
`wait-user-secret` route is removed. The scheduler spends one attempt in the routing tick and the
`owner_notifications` loop recovers the rest, with the terminal endings' bound, spacing and
escalation.

## Catalog installation and engineering exclusion

INSTALL dispatch uses the API's durable catalog-install command and scaffold queue.
Engineering dispatch, direct paid-runs and spawn-worker refuse INSTALL with
catalog_install_not_engineering before executor selection or Run/ledger/budget/worker
creation. An ordinary branch writer refuses catalog_install_in_flight while a
project install is queued, running or requires recovery. Installation shares the
Task/Story/Project lock ladder, coverage/stop/publication fences and current-cycle
ownership. No paid reservation is created or released for a mechanical operation.
[Install settlement](kit-template-and-qa.md#installing-a-kit-package-into-a-generated-product)
owns lease loss, exact-head recovery and explicit retry; engineering retry cannot
replace retained work.
