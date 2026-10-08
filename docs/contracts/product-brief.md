# Product Brief contract

Canonical Product Brief coverage, claim, settings, capability, and dispatch invariants.

Back to the [contracts index](../CONTRACTS.md).

## The Product Brief coverage-to-dispatch boundary

A Story planned from a confirmed Product Brief is released as a whole, not task
by task. `tasks.dispatch_admitted` is that boundary: non-nullable, true by
default — so every task that is not planned against an unadmitted brief keeps
the lifecycle it has always had — and false only for a task created under an
active architect planning attempt. `admit_engineering_dispatch` refuses such a
task with `product_brief_not_admitted`, which is not in `OVERRIDABLE_REFUSALS`:
the release is a property of the plan, not a judgement about one task.

`POST /api/product-briefs/{id}/admit` is the one step that writes the column. In
one transaction, on rows taken for update, it either names the must-requirements
still undisposed (`incomplete`, releasing nothing) or stamps
`coverage_admitted_at`, releases the tasks planned under the presented attempt
and closes it (`admitted`). A second call is a replay, not a rival: it answers
`already_admitted` and releases nothing twice.

An attempt that returns every must-requirement and has no Tasks in its release
set is admitted as a completed disposition, but records `parked` planning
with a typed `planning_failed` reason carrying the refusals. In the same
transaction the Story enters `waiting_human_review` and owes owner/admin notices.
`retry-planning` locks Brief before Story and clears this admission stamp only
when every requirement was returned by that attempt and there are no Tasks with
its `planning_attempt_id`. The shared returned-plan predicate checks this in
both admission and retry; voided Tasks from older attempts do not count.
The confirmed content stays intact; the next claim mints a new attempt and
removes the superseded coverage. Other `planning_failed` stops still retry,
including admitted reopens, preserving the Brief's admission and original plan.
[Recovery of an older taskless admission](../runbooks/returned-plan-recovery.md)
uses the existing planning-outcome and retry-planning actions, without SQL.

Exactly one architect owns an incomplete plan.
`POST /api/product-briefs/{id}/planning-attempts/{claim,heartbeat,finish}` fence
it on the brief row: a claim against an active attempt with a fresh heartbeat
reports `in_progress`, a stale attempt is taken over with a *new* attempt id, and
coverage writes, task creation and `admit` all require the attempt the row names
now. Both the coverage counted and the tasks released are scoped to that attempt,
so a superseded planner's abandoned tasks are never released by its replacement's
admission, and the replacement re-disposes of every requirement rather than
inheriting rows that point at abandoned work.

Confirmed content is never updated in place: there is no update path, and a
change to what the user asked for is a new revision, so an architect planning
against revision N cannot have the ground move under it.

**The brief document has a read shape and a write shape.**
`ProductBriefContent` is what `ProductBriefRead` parses out of the JSON column,
and it stays permissive so that every revision the API has already stored keeps
parsing. `ProposedProductBriefContent` is what `ProductBriefCreate` and
`ProductBriefConfirm` carry, and it refuses what must never be opened as a
revision: a must-requirement id that is not path-safe, and a requirement that
carries neither the user's wording (`user_wording`) nor an auditable reference to
it (`wording_reference`) — exactly one of the two, because both is two answers to
the one question of where the requirement came from. The id refusal is a
property of the coverage route: `PUT /api/product-briefs/{id}/coverage/{requirement_id}`
addresses a requirement as one path segment, so an id carrying `/` would come
back as a 404 that says nothing about why, and the refusal belongs where the
revision is opened. Both are additive: `extra="forbid"` stays, ids stay unique,
and no migration is needed because `content` is a JSON column.

**A brief shows the user how they will use it, in their language.**
`ProductBriefContent` carries `language` (an ISO 639 code such as `ru`),
`usage_examples` (each `UsageExample` names a must-requirement id, what the user
sends and what the product answers), `limitations` (plain-language sentences),
`MustRequirement.user_facing` (default `true`) and `InitialSetting.description`.
All default on the read shape, so a brief stored before them still parses. The
write shape requires a language and a description on every setting, and refuses a
usage example naming an unknown requirement id and a user-facing requirement with
no example. The user-facing text comes from a per-language label table (`ru`,
`en`; any other language falls back to `en`), shows settings only by their
description, and keeps the brief id in the PO-facing prefix. A stored revision
lacking these fields cannot be confirmed; the PO is told to present a correction.

**A choice records the cost of the chosen variant.** `variant_choices` defaults
to `[]` on both content shapes. Each typed entry carries `feature`, `chosen`,
`alternative`, `trade_off` and `add_later`; proposals reject blank fields and
duplicate features. Before presenting a free or simplified variant with a
noticeable quality gap, the PO names the gap and the later upgrade in one
sentence. Both brief forms show each choice on one line in the brief's language,
including that the alternative is not included. Confirmation compares these
fields with the stored content under the existing equality rule. The architect
receives them as recorded context: build only the chosen variant, never the
alternative or its upgrade path without a later order. No endpoint or migration
is added; `content` remains JSON and `limitations` keeps its meaning.

An optional `VariantChoice.capability` names a `cannot` id from the platform
manifest and records the user's explicit acceptance of its workaround. Absent
values are omitted from serialization. PO presentation and confirmation reject
requirements matching manifest detection phrases without that choice. An insisting
user's `pass_capability_request` sends an admin note and starts no work. The
Architect consumer checks the same runtime manifest data before coverage admission:
each conflicting requirement must be returned by the active attempt, never covered
by a task. The return reason names the capability, manifest version and workaround.

**A brief has two forms, both pure functions of the stored title and content**
(`shared/product_brief_text.py`), both Telegram HTML with every user or model text
escaped by `html.escape(..., quote=False)`:

- *Short form* (`render_brief_message`) — the one confirmation message the user
  signs: title and summary, then bold sections *what you get*, *how you will use
  it*, *limitations*, *chosen variants*, *settings* in the user's language, each omitted when empty,
  and the answer line. Each requirement's wording and each usage example appear
  once; no revision or requirement id, no "your words" quote, no provenance, no
  filler. Its length, in UTF-16 units as Telegram counts it, is at most
  `BRIEF_MESSAGE_BUDGET` (3500).
- *Full form* (`render_full_brief_sections`) — every section at full length, with
  the user's own words under each requirement, as a list of sections: heading,
  what you get, how you will use it, limitations, chosen variants, settings (empty ones omitted).
  The PO's `show_full_brief(brief_id)` joins them with `MESSAGE_BREAK` and is a
  `return_direct` tool, so its text is the turn's answer and each section is its
  own Telegram message; a section over the bot's limit is cut by the bot's
  splitter. The admin API returns the same sections from
  `GET /api/product-briefs/{id}/full` as `ProductBriefFullText`
  (`brief_id`, `revision`, `language`, `sections: list[str]`).
- *`show_full_brief` failure rule* — its result is always the full form or one
  fixed apology (`full_brief_unavailable`: the brief's language, else `en`; no
  error text, no id), whatever failed in the read. In a turn where the model calls
  it beside other tools, the PO graph's `post_model_hook` answers every other call
  with a short `Not run:` tool error before the tool node runs, so the brief is the
  turn's last message and nothing else ran; unusable arguments answer every call
  of the turn the same way and the model gets another step.

**Over the budget, the product is staged, and nothing is written.**
`present_product_brief` renders the short form from the *proposed* title and
content before it creates a revision or writes the `product_brief_id` pointer.
Over `BRIEF_MESSAGE_BUDGET` it creates and writes nothing and returns a refusal
naming the measured length and the budget and telling the PO to split the product
into stages — the requirements for a first story in this brief, the rest in a
later one — never to shorten the wording. An open revision stored before the
budget whose short form does not fit is not re-sent; the PO is told to present
its first stage with `corrects_brief_id`. A brief that fits keeps the fingerprint
creation key and the "send the returned message unchanged" rule.

**A proposal is capped; a stored revision is not.** The write shapes
(`ProposedProductBriefContent`, `ProposedMustRequirement`, `ProposedUsageExample`,
`ProposedInitialSetting`, `ProductBriefCreate.title`) cap every count and text:
title 100, summary 400, at most 8 must-requirements (text 200, `user_wording`
250 — a longer quote goes to `wording_reference`), 10 usage examples (sends 150,
answers 200), 5 limitations (200 each), 6 initial settings (description 150), and
2 variant choices (feature 80, chosen and alternative 120 each, trade-off 200,
add-later 160). At
every cap the full form stays under `FULL_BRIEF_CEILING` (12,000 characters as
the user reads them; pinned in `shared/tests/unit/test_product_brief_text.py`),
far below the 20k brief that broke the 2026-09-25 canary. The read shapes keep
their looser limits, so a revision stored before the caps still loads and reads
in full; confirming one is refused like any other over-cap echo, and the PO
presents a correction.

**A brief carries typed initial settings, and never a secret.**
`ProductBriefContent.initial_settings` is an ordered list of `InitialSetting` —
a manifest-declared `key`, an explicit `scope` (`product`, or `user` with a
positive `subject_id`), and a JSON `value` — the same vocabulary the generated
product's core settings contract uses (`codegen-product-kit`, `docs/contracts/product-brief.md`,
"Core settings v1"), so writing them into a product later is a transcription and
not an interpretation. `(key, scope, subject_id)` is unique within one brief. A
credential is not a setting: a credential-shaped key or value is refused by the
type, and the PO additionally refuses a key that names one of the project's
stored secrets, reading only the secret *names*. There is exactly one policy
about credentials on this path, and it lives here; nothing downstream reads a
project secret *value* to seed a setting.

**Those settings reach the product twice: as a plan, and as a value.**

*As a plan.* The architect receives `initial_settings` on its graph state
(`ArchitectState.initial_settings`) and named in its instructions, the same way
it receives the must-requirements. They are not disposed of one by one — that is
what a must-requirement is — because the platform, not the plan, writes them.
For an ordinary service-owned setting, what the plan owes is the declaration
that makes it writable at all: the key declared in the generated product's own
`services/<service>/manifest.yaml` `settings_schema` with a schema the confirmed
value satisfies, and read where the product uses it. An undeclared key is
refused by the product, so an undeclared key means the value the user confirmed
never arrives.

An installed kit package can own this declaration instead. When the selected
package owns the confirmed prefixed product key and declares its product-scope
setting seed, the plan relies on that package contract and the ordinary package
installation/regeneration path. It does not duplicate the key in a service
manifest or add a product DB trigger, startup poller, or product-owned seed.
Ordinary service-owned settings retain the service manifest and generated
registry requirements above.

*As a value.* After a successful deploy of a brief-backed story, the deploy
result handler reads the brief through `GET /api/product-briefs/by-story/{story_id}`
and writes every `initial_settings` entry into the deployed product through the
product's own released `POST /settings/set` — nothing reconstructed from prose,
from project config or from an environment variable, and no second storage
path. Each write is proved, not assumed: `POST /settings/get` must answer with
the key, scope, subject and value that were just written, the shape
`grant_and_resolve` already uses. `SETTINGS_WRITE_CAPABILITY` comes only from
this deploy's in-memory `secret_values`, travels as the `X-Settings-Capability`
header, and appears in no URL, log, error, event, callback, persisted
diagnostic or LLM-facing text. Writing is idempotent by `(key, scope,
subject_id)`, so redeploying the same story writes the same values and ends in
the same state; a story with no brief and a brief with no settings touch
nothing. A commit deploy whose message names no story — the owner-grant deploy
of a fresh project is the only deploy such a product gets — reads the project's
latest confirmed brief carrying `initial_settings` through
`GET /api/product-briefs/by-project/{project_id}/initial-settings` (newest
confirmation first; 404 when none) and seeds it the same way. Every seed step
logs the brief id, settings count and route (`story` or `project`), or that
there was nothing to seed; never a value or the capability.

For a package-owned product setting, that same successful `POST /settings/set`
write invokes the installed package's declared idempotent setting seed inside
the settings transaction. The package declaration, namespaced into the
generated registry under the package prefix, is both the setting schema and the
seed ownership boundary. The platform still performs and proves only its one
set/readback sequence; there is no second deploy seed path.

`DeployRunResult.settings_seed` is the durable record — one
`SettingSeedOutcome` per confirmed setting, naming it the way the product
identifies it plus one closed-set `SettingsSeedFailureKind`, never a value, a
response body or the capability. Every seed failure is
`DeployOutcome.SETTINGS_SEED_FAILED`, and `DeployRunResult` refuses SUCCESS
while any one remains; that outcome itself requires at least one failed seed
record. `SETTINGS_SEED_CONVERGENT_FAILURES` is only a supervisor routing set:
transport, an unclassified set refusal, and refused, malformed or disagreeing
readback may converge on a repeat. If a failed run has any one of those kinds
it redeploys the same commit under the existing bound. That story-wide counter
increments before its ceiling check, so a ceiling of N admits at most N-1
same-commit redispatches. If its complete failure
set is exactly `KEY_NOT_DECLARED`, it instead dispatches one bounded Engineering
repair with the Core v1 manifest guidance; other all-deterministic failures skip
that bound for terminal artifact repair.

`DeployRunResult.settings_seed_needs_manifest_repair` and
`deploy_fix_run_id(source_run_id, attempt)` own that exact repair predicate and
its story-owned Engineering Run identity for both the scheduler producer and
the live harness; they add no serialized result field.

That is its own outcome, not a flavour of `OWNER_ACCESS_PROOF_FAILED`, because
the invariant is **a deploy run may not be reported SUCCESS while a confirmed
setting of its story's brief did not arrive**. `OWNER_ACCESS_PROOF_FAILED`
means "the owner grant was not proved", and the supervisor reconciles it to
SUCCESS as soon as that grant turns out to be applied — which on a brief-backed
first deploy, where the grant is applied before the seed even runs, would hand
QA a run presented as successful with the readback evidence gone. The invariant
lives on `DeployRunResult` itself: a result carrying a `settings_seed` failure
cannot validate as `SUCCESS`, so every producer and
every reconciliation reaches it rather than each remembering to check.
`SETTINGS_SEED_FAILED` has its own supervisor route, which does not consult the
grant-intent lifecycle at all. The seed is idempotent, so a convergent retry
that succeeds writes exactly the confirmed values. An exact Core settings v1
undeclared-key 404 dispatches the bounded manifest repair; a value its declared
schema refuses (422), a product whose environment contract lacks the capability
(an existing pinned product), or any mixed deterministic failure fail the story
with an actionable artifact-repair notification. None is a successful handoff
or a silent skip.

The deterministic 404 and 422 classifications are exact Core settings v1 response
details, pinned against the vendored release fixture. A generic route 404 or
framework-shaped 422 is `SET_REJECTED` instead: it remains bounded retry evidence,
never proof that a product key or value schema is wrong.

*As a fact QA asserts on.* For a brief-backed story the QA run is given the same
confirmed `initial_settings` — key, scope and value — as an established fact,
read through the same `GET /api/product-briefs/by-story/{story_id}`. An
acceptance step about a configured behaviour therefore reads the typed value
rather than reconstructing it from the story description; the canonical case is
`settings.languages = ["ru","en"]`, where the languages QA asserts on are the
confirmed ones and never a list parsed out of prose. A story with no confirmed
brief adds nothing and the run is exactly what it was.

**A QA run can invoke a *named* scheduled behaviour, and judges it on the
product's own output.** The generated product's released core jobs contract
(`codegen-product-kit`, `docs/contracts/product-brief.md`, "Core jobs v1") is the whole of the
mechanism: `POST /jobs/fire` takes a `JobFire` and `POST /jobs/evidence` takes a
`JobCommandRef`, both `contract_version: 1`. Central QA calls them through
`services/langgraph/src/clients/product_jobs.py`, the same narrow shape as the
settings client — nothing here names a module, a queue, a container or a
transport, and nothing here holds the product's capability.

*Where the name comes from.* `shared/contracts/acceptance.py` parses the run's
own checklist. A line `- FIRE JOB <name> [WITH {json}] THEN <observable>` is a
declaration: the name and the arguments are read off it deterministically, and
the observable is what the check is judged on. That is the only source — an
executor never invents a name, never supplies arguments, and `fire_job` refuses
any name the criteria did not declare. A name declared twice is one behaviour
and one execution. A line whose `WITH` is not a JSON object declares nothing, so
a fire the platform cannot spell exactly is a fire nobody may make.

*Who writes the line, and what makes the name answerable.* The architect does,
in the same run that plans the behaviour. When a confirmed must-requirement
implies a deferred or scheduled behaviour, the plan owes two things the product
would otherwise refuse: the behaviour declared by name in the generated
product's own `services/<service>/manifest.yaml` under `jobs_schema`, with an
arguments schema that is `type: object` with `additionalProperties: false` — an
undeclared name is `404` and refused arguments are `422` — and the module that
declares `provides: ["jobs.fire"]`, subscribes to `job_fired` and performs the
work, because the product's core schedules nothing. The checklist line the
architect then authors carries the same name character for character, and
arguments the declared schema accepts. Where the confirmed brief's typed
settings configure the behaviour, the observable is read off those values
(`settings.languages = ["ru","en"]` asserts the output in each configured
language) rather than re-derived from the story prose, and it asserts a
capability rather than a sample: "a digest per configured language" is a check,
"there is a Russian item this week" makes QA red on a quiet week. A story
without such a behaviour gets neither the line nor the declaration. The prompt
and the `update_acceptance_criteria` contract teach exactly the form
`shared/contracts/acceptance.py` parses, and a unit test round-trips the worked
lines through `parse_scheduled_behaviours` itself so the two cannot drift.

*Where the capability comes from.* `JOBS_FIRE_CAPABILITY` is a
`generated_secret` of the product's environment contract. The QA consumer
resolves it on the management host from the project's own encrypted
`config.secrets`, exactly as the deploy path resolves
`SETTINGS_WRITE_CAPABILITY` and the runtime resolves the Telegram credentials,
and hands it to the run's calls. It travels as the `X-Jobs-Capability` header
and appears in no URL, log, trace line, error, event, verdict, prompt or `qa`
CLI argument, and never inside the executor container. Reading evidence back
carries no capability at all. A deployment whose contract predates the jobs core
offers no fire: the run is told so as a fact and a check that needed one fails
visibly rather than being silently skipped.

*Identity and retry.* Identity is `(fired_by_product, command_id)`, the tuple
the product bounds execution on. The runner owns both: `fired_by_product` is the
project under test, `command_id` is `qa-<qa run id>-<behaviour name>`, and
`fired_by_run` names this QA run — the same row the executor's ownership carries
as its attempt. One identity per run per behaviour is why re-invoking the same
logical check within a run is safe: the product returns the recorded evidence
and emits nothing a second time, so a retry can never become a second execution
of the behaviour.

*What a dispatch record is not.* `dispatch_status: dispatched` means the
product's core published `job_fired` — `codegen-product-kit`'s own contract says in
those words that it is not evidence a provider consumed the event or ran the
behaviour. So it is never the answer here either. Every outcome carries that
sentence, the run's established facts state it, the executor prompt forbids
passing a check on it, and the observable named by the criterion — what the
product sent, wrote or now exposes, seen through the ordinary read-only calls —
is what the verdict rests on.

*Central-QA criterion preparation.* Before the executor sees the accumulated
checklist, `services/langgraph/src/agents/qa/acceptance.py` removes only direct
assertions of deployment-owned `POST /settings/{set,get}` seed/readback and the
jobs-core `POST /jobs/fire` transport response. It recognises ordinary Markdown
presentation variants, prose around the call, backticks, and API/version
prefixes. A matching line with `THEN <observable>` is rewritten to retain that
read-only product observable; a valid `FIRE JOB ... THEN ...` declaration is
never removed. All other criteria, including unrelated writes, stay in the
executor checklist. Each drop or rewrite is structured-logged for the QA run's
postmortem evidence.

**The producer of the confirmed brief is the PO consumer.**
`present_product_brief` opens the revision and returns the exact text the user is
shown (the short form, after a PO-only "Product Brief revision N (id: …)" line); `confirm_product_brief` freezes it by echoing that content back.
Recovering the presentation across a PO restart is what the project config key
`product_brief_id` is for — it points at the revision presented and not yet
spent, because until a brief is bound to a story no route finds it from the
project alone; `create_story` clears it after the bind. The creation
`request_id` is a fingerprint of the document being presented — project, title
and content — and never a guessed revision number: the server owns the revision
counter (`max(revision) + 1` per project) and the PO forgets its pointer at the
bind, so a guess would re-spend a key the endpoint already holds and 409 every
later presentation on that project. A revision the key names but that is already
bound to a story is reached past rather than re-presented, so a project's second
brief — the shape of every feature story — is reachable.

**New product work is every story that builds something the user asked for**:
the first story of a DRAFT project and every later feature alike, since a
requirement is lost in prose the same way in both. `create_story` refuses it
without a confirmed brief rather than falling back to the prose `description`
path, and binds the brief through `POST /api/product-briefs/{id}/story`
**before** it publishes `ArchitectMessage`, so the story the architect picks up
is already brief-backed. A failed bind publishes nothing *and closes the story
it could not back*: returning without publishing is not enough, because the
scheduler's liveness sweep re-publishes a `created` story with no tasks and it
would then be planned from prose. The chat PO creates no story without a brief
and cannot choose a story type: a retry after a failure and a complaint go
through `reopen_story` on the original `failed` or `completed` story, which
keeps its brief.

**A planned task's plan membership is immutable while it is unadmitted.** Its
project, story and planning attempt are what the admission's release set and the
coverage evidence are keyed on, so `PATCH /api/tasks/{id}` refuses to change any
of them while `dispatch_admitted` is false, and refuses just as it always did to
move a task *into* a story whose plan is still being built — one fence, both
directions. Without the outbound half, a task moved out of its story would leave
the brief stamping `coverage_admitted_at` over work nothing can release any more,
and a task moved to another project would let a disposition approved under one
project's brief release engineering work charged to another. The task is not
stuck: finishing the plan releases it, and a plan that should not proceed is
abandoned through the planning-attempt fence. Behind that guard `admit` fails
closed — if any disposition it counts names a task that is no longer a member of
the plan, it refuses instead of stamping the boundary over an inconsistency.

**Lock order.** Every Product Brief writer takes the brief row for update before
any Task row. The dispatch admission point reads no brief at all — its condition
is a column of the candidate Task, which rung 1 of `LOCK_LADDER` already holds —
so brief-before-task closes no cycle with task-before-story-before-project.

**The producer of the plan is the architect consumer.** `services/langgraph/src/consumers/architect.py`
is the only thing that claims a planning attempt, and it does so for one reason:
the story it was handed is backed by a confirmed brief. It claims before the
graph runs, heartbeats the claim for as long as the graph runs and stops beating
however the run ends, plans every task under the attempt it holds, records one
disposition per must-requirement through the coverage route, and calls `admit`
exactly once afterwards. An `incomplete` answer is the result of that job:
nothing is dispatched, the story is not moved on, no second admit is attempted,
and the undisposed requirement ids are in the job result and the log whatever the
LLM said about its own run. A failed or incomplete run gives the claim back
through `finish`, which closes the attempt immediately instead of leaving it to
expire with the heartbeat timeout. That does not by itself hand the story to
machinery: this consumer moves the story to `in_progress` before it claims, and
`supervise_stuck_stories` scans `StoryStatus.CREATED` only, so a story stranded
behind an `incomplete` plan is not picked up by today's supervisor recovery and
needs an operator until the scheduler side is widened. The consumer writes
`dispatch_admitted` nowhere and adds no second admission surface; a story with no
brief, or one whose brief is already admitted, is planned exactly as it always
was, and `plan_admission_for_new_task` returns `dispatch_admitted=True` for it.
The scheduler still reads brief state in exactly one place,
`get_product_brief_by_story` over `GET /api/product-briefs/by-story/{story_id}`,
and decides no admission with it.

## Operational overview

`shared/contracts/dto/admin_overview.py` defines the bounded overview payload.
`services/api/src/queue_snapshot.py` owns queue inspection for the overview and
debug route. Missing Redis data is `degraded` or unavailable, never a fabricated
zero. Legacy or invalid executor decisions remain labelled as such and are not
reconstructed from current configuration.

## Explicit catalog selections

plan_install uses the attempt's injected catalog/resources to persist one typed
INSTALL task with the same planning_attempt_id, story/repository ownership and
sequential predecessor as ordinary planning. Coverage refers to this task; it
remains unadmitted until the existing complete-coverage transaction releases it.
The scripted_install_plan harness calls the same tool and coverage API without a
model. Missing or incompatible catalog/dependencies returns a named refusal and
no task. Default binding declares timezone schema without inventing its value;
confirmed initial_settings retains the existing seed/deploy ownership.
