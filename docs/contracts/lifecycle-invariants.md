# Lifecycle and security invariants

Canonical runtime invariants for Run persistence, engineering results, deploy handoff, temporary access, notifications, worker ownership, acceptance evidence, and provisioning observations.

Back to the [contracts index](../CONTRACTS.md).

The [publication and engineering-stop contract](commit-publication-and-stop.md)
defines durable park/terminal settlement before broker ACK, owned retryable stop,
checkout preservation and explicit agent-free handoff. Recovery facts never
rewrite the original paid Run or ledger, and only the named stop can be released.

## Run vocabulary and persisted schema

`RunType` and `RunStatus` in `shared/contracts/dto/run.py` own the Run vocabulary.
Run creation, responses and list filters use those enums; updates may omit
`status`, but an explicit null or unknown status is rejected with 422 before
writing. Unknown create types and filter values are also 422. These checks
preserve the existing paid-run admission and locked terminal-outcome rules.
The `runs` table retains string columns with matching CHECK constraints, so
direct database writers cannot persist another vocabulary.

Schema migrations reject unknown persisted Run values and populated retired QA
observation fields before changing data or constraints; they do not reinterpret
unknown outcomes. Product Brief and requirement-coverage timestamps are required.
The reconciliation preserves known dates, fills a missing date from its paired
timestamp, and uses the migration transaction time only when both are absent.
The API service suite compares the fully migrated PostgreSQL schema with ORM
metadata and verifies the active-incident uniqueness predicates.

## Typed `Run.result` and terminal ownership

`shared/contracts/dto/run_result.py` defines typed deploy and QA result families.
The terminal consumer that owns a Run writes its result while performing the
terminal transition. Supervisors route only a matching typed result for that
Run type; a missing, malformed, or foreign shape is an infrastructure/lifecycle
problem, never a successful or engineering-fix verdict.

## An engineering result carries a new commit or it failed

A DONE engineering result is only a result when its commit changes something on
the story branch. Before the turn is sent, the developer node
(`services/langgraph/src/nodes/developer.py::_pre_attempt_head`) records on the
attempt's `run_metadata` the head the attempt starts from,
`AttemptTurnMetadata.pre_attempt_head_sha`: the story branch head, or the
default branch head when the branch does not exist yet. It is written once; a
reclaimed attempt that adopts an already-pushed turn is judged against the
recorded value, never a head read after the push. Work on the default branch
itself and runs without a branch record none and are not judged.

`_no_new_commit_error` is the one acceptance rule, next to the unpushed-commit
check: a worker success whose reported commit equals that head, or adds no file
change over it (`shared.clients.github` `commit_adds_changes`: nothing ahead of
the head, or commits that net out to no file change), is a failed Run carrying
`EngineeringRunResult.failure_reason = no_new_commit`
(`shared/contracts/dto/run_result.py::EngineeringFailureReason`). A worker success
with an absent or empty SHA, including the consumer's defensive success entry,
carries that same classification. The branch
base, a commit already deployed and an earlier task's commit are all at or
behind the head, so the former default-branch guard is this same check. No
deploy is published for it.

A planning task's attempt that fails this way is an ordinary failed iteration:
the task goes to `failed`, the supervisor retries it while `current_iteration <
max_iterations` and escalates it to human review after that, so it is never
`done`; exhaustion uses the typed story stop and owes both audiences. A taskless attempt
(a deploy repair) has no iteration loop: its story
leaves `in_progress` for human review with the reason on its
`quarantine_reason`, because no pull request can ever be opened for a branch
that carries no commit of its own — GitHub answers that request 422 "No commits
between". `complete_stories` classifies that same refusal through
`shared.clients.github.NoCommitsBetweenError` and parks the story instead of
retrying it every tick; other PR-creation errors stay transient. The consumer settles
the worker turn first and requires the typed story stop before ending the taskless Run.
A refused stop propagates, leaving the Run nonterminal and the queue entry reclaimable;
the scheduler logs a refused stop and leaves the story selected for a later cycle.
An optional engineering callback failure after the stop leaves the committed notice owed.
The empty-result terminal writer retries one transient Run write with the identical typed
result, execution, worker observability and accounting input. A lost response therefore reuses
the immutable outcome and first ledger fact. A persistent write or required worker-settlement
failure propagates without creating a generic terminal answer or another worker turn. A
previously committed matching taskless stop is validated against its cause and both notice
audiences before completing the Run; its original notification episode is retained.
The manifest-repair follow-up deploy Run that an accepted engineering result
creates names its story, so every story-scoped reader of deploy Runs — the live
follow-up wait included — can observe it at all.

## A commit's required derived keys are computable before it deploys

`services/langgraph/src/subgraphs/devops/secret_resolver.py::is_computable_derived_key`
is the one answer to which `derived` keys a deploy computes: the static values,
`CONTEXT_DERIVED_SECRETS`, the port keys and the `*_IMAGE` family. `_compute_secret`
raises `UnknownDerivedKeyError` for any key it rejects, and the deploy skips such
an entry only when it is optional.

Required production derived `PUBLIC_BASE_URL` is the allocated backend's plain HTTP address
and port. Resolver and deployer use the same single backend allocation, independent of resource
ordering; standard-library IP validation and bracketed IPv6 formatting apply. The effective
`ipv4_mapped` address must also pass the same loopback/unspecified/multicast refusal; scoped
IPv6 remains unusable. Usable mapped/native IPv6 and private addresses retain their existing
allocation policy. Resolver, deployer and actual HTTP smoke reuse that validation; smoke adds
exactly one `/health` suffix. Mapped IPv6 HTTP hosts retain their hexadecimal spelling across
Python patch releases, even when `IPv6Address.compressed` changes to dotted notation. Absent, invalid
or ambiguous endpoints fail `environment_resolution_failed` naming `PUBLIC_BASE_URL`.
Overrides cannot replace derived values. The canonical derived entry stays non-sensitive;
other entry kinds retain their declared sensitivity routing. This supplies no domain, TLS,
frontend or webhook guarantee.

For IPv6 allocations, `DeployerNode` uses the authenticated
`GitHubAppClient.get_file_contents` at the full `deployed_commit_sha` to read
`.github/workflows/deploy.yml` and its `.rej` before credentials, repository secret
writes, fencing, temporary tags, dispatch or rerun. IPv4 keeps its existing
admission boundary. `deploy_workflow.py` recognizes a bounded released executable
shape: sole unconditional `deploy` job, `workflow_dispatch`, `ubuntu-24.04` and
the released checkout/key/copy/SSH-action steps in order. A SHA-256 digest of
those parsed steps, excluding presentation names, is tested against the actual
pin render. It includes shell bodies, action inputs and env expressions, so raw
SSH/action host, bracketed SCP host, compose files, target and retry semantics
are established by executable source. Workflow/job/step names, outer YAML
comments/formatting, permissions and job timeout can differ. Extra jobs/steps,
conditionals, aliases, duplicate keys, shell defaults, environment overrides or
unrecognized executable variants refuse; no general shell interpreter is used.

Missing/unreadable source, an invalid built SHA, unknown transport or a retained
workflow `.rej` yields `ENVIRONMENT_RESOLUTION_FAILED` with a bounded cause naming
`.github/workflows/deploy.yml`, `DEPLOY_HOST` and the required reviewed kit update.
Source and HTTP exception bodies never enter diagnostics. Neither answers/version
markers, comments nor a fixed unselected step establish admission. The existing
typed Run and atomic scheduler StoryFailure/owed owner notices preserve this
cause. Reruns remain bound to the same verified built SHA, existing dispatch lease,
publication check, cancellation and temporary-tag cleanup. A subsequent current
merged/published updated target still needs the API-owned grant replacement
authority described above; changing a marker or caller flag cannot reopen it.

`handle_engineering_success` reads the commit's environment contract with the
deploy's own loader (`env_contract_loader._fetch_env_contract`, at the commit
SHA) before the Run is completed, a task is done or a deploy is triggered. A
required production `derived` entry the predicate rejects fails the attempt
through `fail_job`: a failed Run carrying `EngineeringRunResult.failure_reason =
uncomputable_derived_key` and `uncomputable_derived_keys` (required with that
reason and only with it), an error message naming each key, the task `failed`
for the supervisor's ordinary retry, and no deploy Run. The next attempt at the
task keeps its session and TASK.md opens by naming each key and the ways out:
remove it, make it optional with a safe default, or use a `user_secret`. A
repository or contract that cannot be read or validated adds no failure here;
the deploy reports it as before.

## A reused story worker's turn names its task

A story keeps one worker across its tasks, and a reused worker resumes its CLI
session unless the turn carries `clear_session`
(`services/langgraph/src/clients/worker_spawner.py::send_task_to_worker`). For a
planning task's attempt, `services/langgraph/src/nodes/developer_turn.py::plan_turn`
reads the story's engineering Runs — each records its `task_id` and the
`run_metadata.worker_id` that ran it — and:

- sends `clear_session=True` with a TASK.md that opens by naming the task (id and
  title) and saying that earlier tasks in the story are finished and are not this
  task's result, when the reused worker's last turn worked on another task or on
  none on record;
- sends `clear_session=True` with a TASK.md that says the previous attempt at
  this task made no changes, when that attempt failed `no_new_commit` (a fresh
  worker gets the same text; it has no session to clear);
- keeps the session with a TASK.md that names each key, when that attempt failed
  `uncomputable_derived_key`;
- otherwise keeps today's turn: a retry of the same task after any other failure
  resumes its session, and a taskless attempt is sent unchanged.

## A deploy that placed nothing says so

The deploy consumer skips a deploy whose allocation already runs the exact head
SHA under the same environment contract. That skip is correct, and it is a
successful no-op: nothing reached a host, so nothing the deploy would have
applied — a settings seed above all — happened either. The Run still ends
`completed` with `DeployOutcome.SUCCESS`, so across the Run boundary a skip and a
real deployment are the same fact unless the skip is written down.
`DeployRunResult.skipped_reason`
(`shared/contracts/dto/run_result.py::DeploySkipReason`) is where it is written:
`already_deployed_same_sha` names the one skip that exists today, and `None` —
the ordinary case — means the deploy ran. The contract admits the field only on
`SUCCESS`, because every other outcome names something the deploy tried and
could not do.

A reader that needs "the application was actually placed at this commit" asks
this field rather than comparing SHAs itself: an inference reconstructs the
decision the consumer already made and drifts from it. The live settings-seed
follow-up is that reader — a skipped follow-up deploy seeded nothing, so it ends
the wait within one poll with the skip as its reason instead of spending the
repair budget on a result that cannot change.

## Deploy dispatch, withdrawal, and deadlines

`shared/contracts/dto/deploy_dispatch.py` and `services/api/src/routers/runs.py`
own the dispatch record. Claiming the crossing to GitHub Actions and withdrawing
an unclaimed dispatch are ordered under the Run lock. Before the crossing a
stop/revoke may withdraw; after it the recorded lease/deadline drives recovery
and cancellation handling. A revoke that must be the final deploy writer fences
older active deploys instead of allowing an earlier run to restore the value.

## Temporary access

Canonical contracts: `dto/temporary_access.py` and `dto/qa_ssh_grant.py`.

Persist the immutable QA identity and exact deployed-service target before the
capability operation is dispatched. The post-health deploy worker resolves the
generated capability only in `secret_values`, then proves grant or revoke with
the matching access readback. A grant or revoke only ever acts on an existing
deployment of the grant's recorded `target_application_id`: the deploy consumer
reads that application's allocations and never creates any
(`existing_application_allocations`), and never asks the repository for "its"
application, since a repository can hold one per server. When the target exists
and holds no allocations it is not deployed: a revoke completes `SUCCESS` with
no precheck, SSH or DevOps run, because the access went with the deployment, and
the reconciler closes the grant as for any proved revoke; a grant fails
`owner_access_proof_failed` through the ordinary grant retry. A grant, target or
allocation set that cannot be read fails either operation closed with
`owner_access_proof_failed`, never as that proof. Both target columns are NOT NULL: every record has
a target. The target lock and partial unique index scope contention to
`(project_id, target_application_id)`.
Cancelled deploy-lock or fence operations are redispatched against their stored
target without consuming a grant or revoke proof budget; failed, missing, and
stale operations remain independently bounded. Immediately before either remote
operation, the executor re-reads the record and requires its own Run id and
matching in-flight state. Recovery changes that durable operation authority
before withdrawing the predecessor and dispatching fenced cleanup, so a delayed
grant cannot restore access after revoke proof. Cancelled revoke redispatches
retain their attempt budget only before the absolute unrevoked deadline.

`POST /api/temporary-access-grants/{grant_id}/escalate` waits for QA routing.
The routing fact is the server-owned `runs.qa_routed_at` column (the run's own
`story_id` names the story). Its sole writer is `_record_qa_routing`: a story
transition out of TESTING that routes a QA verdict (`complete`, `human-review`,
`start`) names the run in `qa_run_id` and, in the transition's transaction under
the story then QA run row locks, sets it; a transition that names no run leaves
it unset. `RunRead` exposes it read-only; `RunCreate`, `RunUpdate` and the paid-run
command have no such field. Run metadata never proves routing: the reserved key
`qa_routed` (`QA_ROUTED_KEY`) is refused with 422 by `POST /api/runs/` and the run
PATCH (`reserved_run_metadata`) and by `start_paid_run` (`paid_run_reserved_metadata`,
before any audit or Run row). Escalation locks the grant then the QA run; while
that run is terminal with a verdict, linked to a story and `qa_routed_at` is
unset, it answers 409
`qa_routing_pending` (`QA_ROUTING_PENDING`) and writes nothing. The reconciler
then sends no alert and redispatches the revoke without spending an attempt; it
asks again when that revoke fails. Nothing else stands in for the column: not a
newer QA run of the story, not a story status move. A QA run with no verdict yet
still receives the routable `qa_cleanup_failed` blocker.

`POST /api/temporary-access-grants/{grant_id}/drain` is the sole unproved-close
boundary. Under the grant row lock it accepts only a complete target-backed row
already stamped `revoke_failed` and escalated by the bounded reconciler; the generic lifecycle update cannot stamp escalation. It
writes `revoked`, `revoked_at`, the typed
`operator_drain` reason, and one actor audit in the same transaction. Equal
repeats return the settled record without a second audit. It does not prove that
remote access is absent and cannot override an ordinary current-format lifecycle.

## QA handoff and restricted access

`dto/qa_handoff.py` binds QA to deploy provenance. Health-only criteria run over
HTTP without an executor. Other criteria use the central ephemeral QA executor
through worker-manager, which is QA's only executor: when it does not run, the
run ends as a typed infrastructure outcome rather than retrying elsewhere. The executor receives a run-scoped restricted capability, no target SSH
credential, and egress only through the assigned proxy, whose per-run allowlist
is the model backend, the host of `WorkerConfig.qa_target_url` (the deployed
public URL, sent as data and refused by worker-manager when it could name the
platform) and Telegram's data centres. The network carries any request to the
target; direct application-API writes stay forbidden by the QA instructions and
the runner's write guard until product-data isolation exists. The QA Telegram identity is served to the
executor by the capability endpoint (`telegram_identity`) only after the run
proved it; the outcome is `Run.run_metadata.qa_telegram_identity`
(`handed_over`, and on refusal `reason`/`detail`). Failure to establish
that boundary is a typed infrastructure outcome, not a product verdict.
The fixed Telegram tools `telegram_probe` and `telegram_click_button` stay
platform tools in qa-worker (sprint:1464 option B), not routed through the run's
proxy: their child scripts are platform-written with every input a JSON literal
(proven by executing them with hostile values), and they run only with the
proven identity — without it each returns the `missing_telethon_credentials`
blocker preflight uses and starts no child process.

QA parses criteria before it resolves exploratory-only resources. Deterministic
probe inability, unavailable target runtime, bot liveness failures, access
denials, and product check failures retain distinct typed classifications.
Only a typed failed product result is eligible for an engineering fix loop.

`servers.ssh_user` is the administrative account the fleet key opens, and every
contour registers the same one: the QA grant writes *another* account's
`authorized_keys` over that connection, which no non-administrative account can
do. The one-shot grant and revoke scripts carry typed exit statuses: `3` no such
account or home, `4` no `.ssh` or no `authorized_keys`, `5` the connection could
not read or rewrite them. `4` is a claim about the target's provisioning and is
journalled as one; `5` is a claim about the connection, is classified
`qa_identity_unreadable`, and is never reported as an absent seat. That category
is operator-recheckable like the other repaired-outside-the-code QA
infrastructure blockers: its repair is the server row's administrative account. A revoke
answers zero surviving keys only from a file it read: an unreadable one is
residue the cleanup verdict carries, never a clean result.

## Host-side scripts and the system interpreter

Every `python3 -m scripts.<module>` line in `.github/workflows/stand-e2e.yml`
runs under a machine's system interpreter — the runner's, and on the target the
one behind `ssh ... python3` — never the repository environment, which is not
guaranteed to exist at that point in the workflow. Such a module, and everything
it imports transitively, may use the standard library only. Reaching into
`shared` is allowed exactly as far as that holds: `shared.provisioning_policy`,
`shared.stand_credentials` and `shared.constants` are stdlib-only and are on
this path; anything importing `pydantic` — `shared.contracts` above all — is
not. `ADMIN_SSH_USER` lives in `shared.provisioning_policy` for this reason, so
`ServerCreate.ssh_user` and the stand registration share one constant without
the script importing the model. `tests/unit/test_host_script_interpreter.py`
enumerates the invocations from the workflow file and imports each module with
site-packages off; a new invocation is covered by that parse automatically.

## Terminal owner notification

`dto/owner_notification.py` and `shared/contracts/queues/po.py` define the
handoff. Persist the owed owner notification before PO publication; the
lifecycle-wait notices (`dto/lifecycle_wait.py`) are persisted by the API in the
transaction of their move. Recovery
publishes the durable obligation once; it does not turn a duplicate queue event
into a second owner notification. A deployed address is included only when the
typed lifecycle state authorises it.

## Engineering rollout, drain, and adoption

`dto/engineering_consumer.py`, `shared/contracts/worker_turn.py`, and the
engineering consumer own rollout continuity. A drain is stored and audited
before a consumer stops claiming work. Replacement consumers reclaim/adopt the
same compatible PEL turn rather than publish a duplicate prompt. A terminal
settlement tears down unconsumed turns only through their ownership fence.

Operator inventory distinguishes Docker observation, active turn lease, story
binding, and waiting attempt. An unreadable source is unavailable, not absent;
a live but unowned container must not appear healthy.

## Worker ownership, teardown, and removal evidence

`queues/worker.py`, `shared/contracts/worker_evidence.py`, and
`shared/contracts/worker_control_plane.py` are canonical.

Every dynamic worker carries non-empty project, initiating-run, and attempt
ownership. The create path records ownership before a container can exit. A
delete path captures durable removal evidence before removing the container and
before deleting worker metadata; if durable attribution cannot be written, the
last metadata name is retained rather than silently losing the worker from run
evidence. Cleanup selects only the owning run's labels, verifies removal, is
idempotent, and refuses an unscoped or neighbour-owned resource.

Developer workers store no GitHub token in Docker environment, Git configuration,
remote URLs or credential files. Native Git get requests credentials through the
authenticated broker operation; store/erase persist nothing. `useHttpPath` binds
the request to the platform-owned repository. gh receives auth only for one child
command, and its persistent auth subcommands are unavailable. No turn token is cached.

Before every developer turn, including reuse, the wrapper runs `git ls-remote origin`
through this helper. Failure submits typed pre-agent infrastructure evidence before
workspace preparation or any runner/model call. The Run and Story follow the existing
infrastructure parking path. Publication still uses non-force push and exact SHA
readback. Existing containers must be drained and recreated before activation;
preserve unpublished work and ownership as specified in [the credential upgrade
procedure](../SECRETS.md#worker-credential-upgrade-procedure).

## Paid-run acceptance evidence

`tests/live/run_evidence.py` and `scripts/stand_acceptance.py` are canonical for
the acceptance artifact a `stand-e2e` run publishes.

The workflow resolves its suite through `scripts.stand_run.resolve_suite` before
credential-dependent steps. `requires_model_sessions` beside that resolver allows
session bypass only for a resolved name registered in `SUITES` with `llm=False`.
Aliases resolve first; raw/unknown targets and absent selections require sessions.
`Suite.llm` still selects the developer/QA executor environment, which raw targets
retain unchanged even when they make other model calls. Standalone credential and
runtime preflight commands retain full session checks when no suite is specified;
admission then requires profile protection, and an explicit empty suite is refused.

A registered no-model suite, including `mega-noop`, restores,
installs and persists no model profile and performs no authenticated model
probe. Exact image release, CLI version and numeric image identity checks remain
mandatory. Runtime preflight receives the same suite and skips only unused model
session checks; service, GitHub App, SSH, registry and provider requirements remain.

Paid suites and raw targets authenticate before provisioning and persist refreshes before any
machine is created and after runtime use. Their handoff scans initial and rotated
profile tokens and attests that scan; cleanup requires that attestation. No-model
handoffs require no profile attestation, scan all supplied protected values, and
still fail admission if any required non-session secret is absent.

The selected-suite SSH invocation records workflow/run/revision/suite/host identity
and its actual exit status on the runner. Collection copies that log and the public
machine manifest before remote transfers, records transfer failures, and keeps
available runner evidence. Missing runtime reports still make acceptance incomplete;
an SSH refusal says nothing about a provider deletion actor or account cause.

**A paid run's artifact always carries the three captures.** Per worker the run
created: the transcript body (`transcript.content`), the agent's final report
(`agent_report`, the `worker_report` task events of this run's engineering
tasks) and the diff of the branch that worker produced (`branch_diff`, named by
repository, branch and head SHA). Each is present or carries the stated reason
it could not be collected; none is ever a bare empty value, and a QA executor —
which writes no report and produces no branch — says so. There is no condition
on the outcome, and `scripts/stand_acceptance.py` demands them of any paid
artifact rather than of one that classifies itself as failed. A free
deterministic run spends no subscription and retains none of them: its
transcript is named by path and file list only.

**Every run's artifact carries what each engineering attempt was told.** Not
just a paid one: `developer_instructions` holds, per engineering **task** of the
run, the `TASK.md` worker-manager injected into that attempt's workspace and,
where the attempt has one, the `.story/STORY.md` beside it. An attempt is a task,
not a container — a story's worker is reused across its tasks, so `run.attempts`
(containers) is a different count — and worker-wrapper rewrites
`/workspace/TASK.md` at the start of every turn, destroying the previous
attempt's document in place. So every evidence pass the run already takes reads
the workspace off the host side of the container's own bind mount and keeps what
it read, distinct by digest; a reading is attributed to an attempt by that task's
own description appearing in it, and the story document by the reading written
closest to it. Nothing holds a container open or moves teardown to make this
possible: a document no pass reached before removal is reported unread.

A document is `captured`, `absent` (the attempt was given none — ordinary for
`.story/STORY.md`, a gap for `TASK.md`), `unreadable` (with the reason) or
`not_applicable` (a QA executor is not an engineering attempt).
`developer_instructions.complete` is a missed capture naming every gap, so the
section cannot read as complete while a document is missing. Per attempt,
`acceptance_criteria` states whether the task's acceptance criteria appear in the
captured `TASK.md` as one exact substring — `format_acceptance_criteria` writes
them there stripped and otherwise untouched.

Two helpers read that, and the difference is the assertion:
`attempts_not_quoting_acceptance_criteria` counts only `not_quoted`, so an
attempt whose task carries no criteria is not counted — the right question only
for a scenario whose tasks genuinely may have none.
`attempts_without_quoted_acceptance_criteria` counts everything that is not
`quoted`, so an empty list is two claims at once: every engineering attempt *has*
acceptance criteria, and each attempt's `TASK.md` quotes them. The level-1 live
suite asserts the second, and `admit_level1_plan` plans both tasks with the
criteria QA checks them by, keyed on that run's marker — so a `TASK.md` left over
from another run cannot satisfy the check, and a plan that asked for nothing
cannot pass it.

Unconditional because the condition could not be evaluated where the artifact
must be written. A `stand-e2e` run's result is decided outside the pytest
process and after it — `scripts/stand_run.py` fails a run on a sweep error after
every cell passed, and SIGKILLs pytest on its hard timeout — so no in-process
signal can gate what the artifact carries.

**What the artifact classifies, and what it does not.**
`run_evidence.run_failure` answers one question — did this *combination* succeed
— from pytest's per-test verdicts and session exit status, both recorded by the
live conftest, and from a pipeline that did not complete. `failure.failed` and
`verdict` report that and nothing wider. `failure.stage` and
`failure.failure_kind` answer a different question, *where the pipeline
stopped*, so a run whose pipeline completed and whose suite then failed keeps
`stage: completed` while `failed` is true, `failure.source` is `suite` and the
verdict is red with a `suite_failed` reason. A **runner-level** outcome — a
failed sweep, a hard timeout — is the workflow's verdict, is not observable from
inside the run, and is not represented in this classification at all.

The bodies are collected, redacted and held before teardown, because that is the
only moment they are readable. The artifact is written there too — a
crash-safety copy — and rewritten in place at `pytest_sessionfinish` once the
in-process suite verdict exists.

Every retained byte is redacted on the stand host by
`shared.diagnostics.redact_diagnostic` before the artifact leaves it and before
any bound is applied, line by line, against every value of the harness process
environment whose name says it is a secret. Redaction precedes bounding: a cut
taken first leaves a straddling value unmatchable and publishes its prefix. A
body carrying a protected value that spans a line break is withheld with a
stated reason, and a redaction that does not complete publishes its stated
reason instead of its input. `FAILURE_RETENTION_MAX_CHARS` bounds the redacted
text, and a truncated body says in the artifact that it was truncated and at
what limit. The admission fails closed on the three captures for every paid
artifact, and on the stage, the reason and the reachability reads for one that
reports a failure.

## Provisioning and environment observation

Infra-service owns provider observation/client code; policy decisions stay in
`shared/provisioning_policy.py`. Provisioning success is not inferred from a
request publication. Environment observation reads the running target through
the infra boundary and reports its typed outcome; it does not read a repository
file or assume a dispatch changed the deployed service.

`ProvisionerMessage.profile` is an optional, typed, request-scoped execution
profile. The absent default is the full production provisioning path. The only
current override, `stand_e2e`, is admitted by the internal provisioning request,
carried through the queue, and consumed by infra-service for the disposable
Stand target. It is not inferred from mutable server labels; a replay retains
the profile that was originally queued.
