# Commit publication and engineering stop

`dto/commit_publication.py` owns publication evidence, exact-SHA recovery input,
the server-derived recovery identity, claim/receipt reads and engineering stops.
`worker_commit_not_published` is a failed engineering outcome, with the original
worker report and execution/provider facts. It is never an infrastructure retry.
Missing branch/object or failed inspection carries no invented verified SHA.
Git stderr is redacted and bounded to 2,000 characters. Credentials are excluded.

## Authority and ordering

`services/api/src/attempt_disposition.py` decides from locked facts: committed
human review/stop, then unpublished work, then ordinary eligibility. Paid Task
admission, taskless paid admission, retry/reopen/start/completion, consumer
reclaim, worker creation, broker leases and worker turns reach this authority.
Final deploy start/dispatch claims also fence stopped Stories and held checkouts.
Client actor strings and queue payload identities grant no authority.

Explicit conflict, infrastructure and planning recovery commands can name `stop_id`.
Under the native action's existing ownership, cycle/attempt, quarantine and
budget proofs, the API releases exactly that stop and audits the authenticated
actor in the same transaction as its transition. Automatic conflict callers
select no stop and cannot release one. PO reopen and the admin infrastructure
buttons carry the stop they displayed; a replaced stop refuses. Acceptance and
QA recheck release their reviewed stop before their native transition.
`attempt_disposition.release_engineering_stop` is their shared release owner,
including paid Task resume and verified publication handoff. It compares the
validated cause snapshot and exact stop, retains a different publication hold,
and audits release before the guarded native transition in the same transaction.
Stale stop IDs refuse even when the current stop was already released.
Story stop precedence returns `engineering_stopped`; a standalone Task carrying
only infrastructure evidence retains `infrastructure_parked`, without a Run.

| Explicit release consumer | Native proof before release | Preserved facts |
|---|---|---|
| `repair_pr_conflicts` | Current project/PR/cycle, dirty GitHub head, eligible unspent or released refusal episode, exact selected stop | Repair admission, bound, spent attempts and prior notices |
| `retry_infrastructure_attempt` | Equal typed Story/Task park, named attempt/refusal, owned pre-agent Run or admission refusal, legal reset | Current iteration; free refusal accounting and retry audit |
| `retry_story_planning` | Typed planning failure, native bounded retry record, selected stop | Planning attempt history |
| `resume_task` | Parked Task, no publication/infrastructure hold or live sibling, current conflict admission where applicable, selected stop | Paid history; explicit fresh iteration and new bound audit |
| `accept_story_result` / `_complete_story` | Authenticated administrator, reviewed current reason, reachable recorded QA target | Acceptance snapshot; completed owner notice |
| `recheck_story_qa` | Authenticated administrator, current QA episode, owned application and exact deploy receipts | Recheck snapshot; original verdict and native deploy handoff |
| `recover_commit` / `_handoff_recovery` | Locked current attempt/cycle/iteration and checkout identity, exact published receipt, matching project hold and stop | Original failed Run/ledger; one claim and native completion |

Discovery reads only identities. Writers take ascending Task roster, Story,
Project, ascending engineering Runs, then the recovery claim. Roster growth
refuses the operation for a fresh discovery. Task-only hops take their Task then
Story and do not inspect siblings. Project serialization also rejects recovery
after a newer engineering attempt in another Story reused the shared checkout.
Recovery also locks its Repository identity before external publication, so a
concurrent repository edit cannot substitute a target during proof.

The API holds the Story fence through worker-command/turn XADD. Redis atomically
stores the stream entry and its request receipt; HTTP replay publishes no extra
prompt. Commands admitted before a stop remain subject to manager/lease checks
and owned teardown. A committed stop prevents later command/turn publication.

## Publication delivery

Wrapper completion and explicit recovery both call `shared.commit_publication`.
It resolves the named commit, requires exact local HEAD and branch, checks the
owned origin and push URL in recovery, inspects injected paths, and validates the
persisted attempt baseline. It pushes the resolved SHA without force and accepts
only exact `ls-remote` proof. Readback may prove an already accepted push after
a lost reply or timeout. Recovery cannot substitute a changed HEAD.

Before forwarding a refusal, broker stores the strict failed worker output at
`engineering:publication-pending:<attempt>`. Manager authenticates independently,
checks the active lease and persisted Run, and binds the owned Repository.
The API parks Task/Story/Project and settles a live Run through the existing
terminal timestamp/accounting writers in one transaction. Terminal outcomes and
ledgers already recorded remain immutable; supplemental worker facts are retained
as `publication_worker_result`. Generic Run patches cannot forge/clear these keys.
Task failure evidence and the Project checkout hold are likewise protected.

Failed SQL delivery retains the Redis output and input lease. Timeout/removal
settlement consumes this exact output, including provider facts, before generic
failure handling. Stop reconciliation does the same after owned removal proof.
Broker output, input ACK and immutable body receipt are atomic; same-body replay
after a lost HTTP response creates no duplicate output. A changed body refuses.
Developer teardown preserves its checkout; GC preserves local untracked commits
and fails closed when native Git cannot establish their absence.

Taskless `no_new_commit` settlement retains an `EmptyEngineeringTerminal`
payload in the owned live Run's `empty_result_terminal` metadata before stopping.
It contains the exact failed result, error, execution/provider ledger input and
transcript references. Retention fences launch even before the stop commits;
teardown cannot replace it with unknown cancellation. The API validates and
preserves it, and queue reclaim finishes only that terminal write and its stop,
without executing the graph. Terminal Run/ledger immutability still applies.
Null replacements refuse, and terminal settlement requires the exact retained
result, error, accounting and supplied transcript/profile facts together.
Result equality is typed: omitted unset optional fields and explicit nulls for
those fields serialize the same outcome. The owner persists its canonical result
before terminal immutability checks; changed facts, malformed shapes and cleared
results still refuse, including on replay.
An unrelated stop or notification episode is never rewritten into an empty
result stop; identical stop retries preserve their original cause and notices.

## API actions

All following actions require internal credentials or an authenticated admin.
Stop/recovery/resume actors come from those credentials.

| Surface | Request and result | Ownership and delivery |
|---|---|---|
| `POST /stories/{id}/human-review` | Existing stop body; Story read contains `engineering_stop` | Commits stop/audit first; retries owned worker teardown through the scheduler |
| `POST /stories/{id}/reconcile-engineering-stop` | Pending teardown count | Same stop and owned worker IDs; missing removal proof remains pending |
| `GET /engineering-stops/pending` | Stopped Story reads | Scheduler redrives unreleased stops across all Story statuses |
| `POST /runs/{id}/engineering-disposition` | `AttemptDispositionRead` | Live admitted attempt, persisted Project/Story and initiating Run |
| `POST /runs/{id}/publish-worker-command` | Native `CreateWorkerCommand` | Validates admitted ownership and fences XADD under the Story lock |
| `POST /runs/{id}/publish-worker-turn` | `EngineeringTurnPublication` | Validates persisted worker and Redis ownership; atomic request receipt |
| `POST /runs/{id}/park-publication` | Strict `WorkerFailedResult`; `CommitPublication` | Parks and settles through the native terminal/ledger writers before ACK |
| `POST /stories/{id}/recover-commit` | `CommitRecoveryCommand`; `CommitRecoveryRead` | Exact SHA, original failed Run, current cycle/iteration, owned checkout, exact current stop |
| `GET /commit-recoveries/{attempt}` | `CommitRecoveryRead` with typed identity | Durable claim, refusal/proof receipt and handoff timestamp |
| `GET /stories/{id}/recovered-commit` | Current handed-off claim or null | Native Story completion can discover a recovered taskless commit without creating a Task |
| `POST /tasks/{id}/resume` | Existing resume body plus exact `stop_id` | Separately audited paid budget/iteration action; unpublished work requires recovery |

## Recovery persistence and handoff

Migration `e5a7c9d1f3b6` adds nullable `stories.engineering_stop` and
`commit_recoveries`, keyed by the original engineering Run. API stop/recovery
are the only writers. Manager reads the committed claim through authenticated
API calls; scheduler reads successful handoff for normal Story discovery.
Claims contain identity, SHA, actor, stop, receipt and handoff time, never tokens,
paths, a new Run, a budget reservation or a second accounting fact. Foreign keys
cascade with explicit deletion of their parent Run/Story.

Claim, publication receipt and handoff are separate commits. Each continuation
reacquires the same ladder and revalidates cycle, iteration, newer attempts and
stop identity. Concurrent/repeated requests for the same claim return its durable
result. A different SHA or stop refuses. Missing ownership, workspace/object,
wrong repository/branch, changed HEAD, injected/no-new content, non-fast-forward,
credential refusal, timeout and readback mismatch never authorize handoff.

Manager derives the direct checkout child from the owned Repository ID, refuses
a live workspace lease, obtains `get_repo_scoped_token` for that repository at
each execution and passes transient native Git configuration via environment.
Released headers/helpers are overridden; tokens appear in neither argv nor
files/logs. `WORKER_MANAGER_URL` is required API connectivity configuration.
The base Compose file supplies the native manager endpoint; production and
stand inherit it. Independent service/integration API containers, including
image-only `api-factory`, explicitly supply their synthetic topology endpoint.
Unit fixtures and service-image import checks declare theirs independently;
absence still fails startup. `.env.example` documents standalone connectivity.

Verified handoff uses the native audited Task completion owner, releases only
the named stop, clears this attempt's checkout hold and returns Story to
`in_progress` atomically with `handed_off_at`. The native `complete_stories`
owner discovers it, resolves the current-cycle PR and retains PR/CI/merge/deploy
ownership. The failed Run and ledger are unchanged. Recovery starts no worker,
executor, coding turn or paid admission. Replays never repeat Task handoff.

Legacy work requires explicit `adopt_preserved_commit=true`, complete persisted
worker/initiating-Run/baseline and Repository ownership, and native exact local
and remote proof. Prose is never evidence. A bundle must be restored separately
into the trusted checkout by its operator; this action does not clone/reset it.
