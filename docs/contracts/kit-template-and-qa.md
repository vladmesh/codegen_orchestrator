# Generated product kit and QA contracts

Canonical template, package-installation, central QA, executor transcript, probe, and QA capability contracts.

Back to the [contracts index](../CONTRACTS.md).

## The template a project is scaffolded from

`shared/contracts/template.py` owns the pair `ScaffoldMessage` carries.
`ServiceTemplateSource` is a `Literal` over owner-controlled repositories only —
`gh:vladmesh/service-template` and `gh:vladmesh/codegen-product-kit` — and that
literal is the whole admission: the scaffolder runs `copier copy --trust`, so a
source it accepts is a source whose Copier tasks run. Adding a repository here is
therefore a decision about executing that repository's code, not a configuration
change. `ServiceTemplateRef` refuses a floating ref (`HEAD`, `main`, `master`), so
the ref names an immutable tag or commit and the render is reproducible. Which of
the admitted sources production uses is separate and lives in
`scripts/system_configs.yaml` (`scheduler.service_template_source/ref`).

That seed is the single definition of the pin: it is what a deployed orchestrator
reads, so nothing else in the repository writes the source or the ref down again.
Production scaffolds from `gh:vladmesh/codegen-product-kit`, pinned by an immutable
commit of that repository and no longer from `service-template`.
The production boundary is kit main commit `52e9107495949c9187f41cc0e50367ed8ed7a7a1`
(kit card 54, merged-main Runner 38010408741 green on it). Rendered at a commit, Copier
records `_commit` in `git describe` form (`packages/tg-channels/v0.1.2-12-g52e9107`);
`shared.contracts.template.recorded_template_commit_matches` and the install probe accept
exactly the pin itself or that form of it. The matching
`shared/tests/fixtures/codegen-product-kit-52e9107495949c9187f41cc0e50367ed8ed7a7a1` tree is
its `backend,tg_bot` Copier render. The root `codegen-kit-tooling` dependency, the LangGraph
requirement and their locks resolve the same commit; the locks are the CI producer's output
(`.github/workflows/template-fixture.yml`, job `locks`), never hand edits.
It represents the committed checkout: generated ignored `.env` and `TASK.md`
are omitted; every versioned rendered file retains the producer's bytes. It is
rendered with `copier copy --trust --defaults --vcs-ref=<tag>` and the answers
its `.copier-answers.yml` records, and only the files a fresh `git add -A` of the
render would track are vendored; the `uv.lock` Copier's task writes resolves
third-party packages at render time.
Its main-push image workflow prepares the root, `services/backend` and `services/tg_bot`
environments (`scripts/prepare-env.sh`, frozen syncs) before generation and before building
either service image.
The render carries bigint user identifiers, forward migration `e6b8c2d4a901`
after `d4a7b2c9e1f0`, and Telegram token protection in HTTP logs, as since the
kit's [0.6.3 release](https://github.com/vladmesh/codegen-product-kit/blob/f23460c62fa3508858c0552557b2860af09f2656/docs/releases/0.6.3.md).

This commit carries core facade `2.5.0`, protocol `1` and tooling distribution `0.1.0`:
the core-owned product `language` setting, one authoritative bot command registry, and the
typed `kit check-install` preflight, on top of generic platform environment sources and
finite bilingual binding v2 (v1 unchanged).
The retained fixture was rendered in [CI run 38012764349](https://github.com/vladmesh/codegen_orchestrator/actions/runs/38012764349);
[producer hashes](../evidence/catalog-install-fixture.json) cover every tracked file and the saved answers.

Failed Actions evidence keeps the earliest root diagnostic and final error in at most
`scheduler.ci_failure_log_excerpt_lines` data lines (40), plus at most two omission separators.
Root diagnostics include pytest assertion lines, Python exceptions, explicit error annotations
and lint findings; teardown `make` errors and pytest `FAILED` lines are final diagnostics.
Run-step boundaries restrict diagnostics to the last failing step. A pytest window starts
at its preceding test header in the FAILURES section; a header too distant from the
assertion uses a separate window, with a separator before the assertion context.
Runner exit-code wrappers and structured retry noise do not displace a cause. A one-line
budget retains the cause; output without diagnostics retains the tail. Lines are clipped
to 2048 characters; the data budget is reduced when needed to keep the total within
131072 characters, including newlines and separators.

`shared/contracts/env_contract.py` mirrors the pinned tooling model, including strict service,
scope and quota validation and the Python-only credential check for platform URLs. Schema
equality and a validation corpus test both copies. Production `platform_base_url` resolves
the declaration's URL. Production `platform_key` issues a `cps_<id>_<secret>` credential
(12 lowercase base32 characters, 32 random bytes as unpadded base64url) through the generic
auth admin client. The product id is `orch-` plus the first 58 hexadecimal characters of
SHA-256 over the UTF-8 orchestrator project id: stable across title/slug changes, 63 characters,
and valid for auth's product-id pattern.

The resolver obtains/reuses keys, persists newly generated values through the existing
encrypted project-secrets merge under the deploy fence, then registers product, grants and
keys. Each admin write rechecks the fence. Every product/grant PUT sends `If-None-Match: *`;
412 succeeds without replacing operator-disabled status, scopes or quotas. Keys register
idempotently. A stored key revoked in GET, or revoked in the registration response, is
replaced and persisted before registration; repeated revocation returns a retry.

`PLATFORM_AUTH_ADMIN_URL` and `PLATFORM_AUTH_ADMIN_TOKEN` are read from LangGraph settings
and required at issuance. Missing configuration gives `ENVIRONMENT_RESOLUTION_FAILED` with
`platform_service_unconfigured`; 401/403 gives the same outcome with
`platform_auth_unauthorized`. Other request/response refusals are configuration failures.
Transport errors, timeouts, 429 and 5xx give `DeployOutcome.RETRY` with
`platform_auth_unavailable`, which the existing supervisor redeploys under its retry bound.
None of these failures asks for a user secret. Diagnostics exclude response bodies, plaintext
keys and tokens. Production network/env wiring remains separate work.

Stand alone may set `PLATFORM_BASE_URL_OVERRIDE`, an HTTPS template with one
`{service}` path slot; settings require `LIVE_CONTOUR=stand` and production deploy
refuses a non-empty override. Only base URL resolution changes: key issuance still
uses the same persistence, fencing and auth admin client. The stand overlay runs
an in-memory fixture platform from the released API image, with internal admin
authentication and TLS service ingress. Service requests require registered,
unrevoked keys and service grants; fixture data declares routes and JSON responses.
The shared admin contract table covers both the issuance unit fake and stand app.

Historical core `2.1.0` introduced by `0.7.0` added these product behaviors;
`0.7.1` fixed lifespan tests with installed packages:

- **A core timer loop.** The backend fires the timers an installed package declares in its
  manifest, once per slot through the ordinary jobs path (`command_id`
  `core-timer:<job>:<slot instant>`, `fired_by_run` `core-timer`), so reminders `0.4.0` fires
  `reminders.tick` every 60 seconds in production with no external caller. A product without a
  package timer starts no loop.
- **Verified caller identity for package routes.** A package route depends on
  `codegen_kit.caller_identity`, which requires `X-Identity-Capability` equal to the backend's
  `generated_secret` `USER_IDENTITY_CAPABILITY` together with `X-User-Channel` and
  `X-User-External-Id`, answers 401 or 403 otherwise, and hands the route the canonical
  `user_ref` `<channel>:<external_id>`. `USER_IDENTITY_CAPABILITY` is declared in the backend env
  contract with consumers `backend` and `tg_bot`; the deployment secret resolver generates it like
  every other `generated_secret`, and the bot reads it from the deploy `.env` through its compose
  `env_file`. The generated bot calls package routes through
  `BackendClient.request_as_telegram_user`.

Reminders `0.4.0` is a breaking change for its callers: `/reminders` takes no `user_ref` in a body,
query or path and acts only for the verified caller, while the stored `user_ref` and the
`reminders.due` payload carry the canonical form. The `reminders.reminder_owner_ref` setting stays
opaque, but only its canonical form (`telegram:<id>`) lets that user see the seeded reminder.
Central QA reads those routes as one verified QA user (see "Central QA reads package routes as a
verified QA user" below). `POST /jobs/fire` of `reminders.tick` with `X-Jobs-Capability` is unchanged.

This pin changes new-product scaffolding; it does not migrate deployed products.
Existing products need a reviewed Copier update on a clean review branch:
`copier update --defaults --trust --vcs-ref=<reviewed-release> --conflict=rej`. Preserve selected
modules and owned application/spec/environment bytes, back up ignored real
environment data through the product's restricted procedure, and compare its
bytes locally without exposing credentials. Read back answers/source, tooling
revision, both frozen environments and the full workflow. An exit of zero and
new answers can coexist with `.rej`: Copier installs the candidate workflow
while retaining rejected local hunks in the artifact and committed predecessor.
Reconcile each hunk deliberately, retain reviewed unrelated customizations,
resolve rejection artifacts and validate the product before its normal reviewed
merge and image publication. Copier keeps the product-owned
`services/backend/src/app/lifespan.py`, so the timer loop has to be reconciled into it by hand, and
`USER_IDENTITY_CAPABILITY` has to be added to the preserved `.env` files. No bulk updater or remote
workflow patch exists. The immutable
[release notes](https://github.com/vladmesh/codegen-product-kit/blob/1de7aa6c02cfcf212b2d21919defbb3d77383998/docs/releases/0.7.0.md)
describe this boundary and the update steps; the
[0.7.1 notes](https://github.com/vladmesh/codegen-product-kit/blob/56da5c83cb8d011823ce2cb70345415b223b93ab/docs/releases/0.7.1.md)
add the test fix a `0.7.0` product with an installed package takes. External action execution, remote authentication and
production deployment are outside the nonconnecting validation boundary.
`scripts/template_pin.py` parses it and every other site derives from
`TEMPLATE_PIN` — the live suite's scaffold defaults
(`tests/live/pipeline_helpers.py`, still overridable per run by
`LIVE_TEMPLATE_REPO`/`LIVE_TEMPLATE_REF`, both or neither), the stage-5 template
smoke, the vendored render's directory name
(`shared/tests/fixtures/<template repository>-<ref>/`, named after the pinned
source so a move renames it) and the CI gate's exclusion for it. Moving the pin is
therefore one edit in the seed, and
`tests/unit/test_template_pin_single_source.py` holds that shape: the ref is a
literal in that file alone, and a moved definition reaches every derived site.

The pin is the kit's release tag, so Copier's clone reaches it and records that tag in
`_commit` — the pinned ref itself, which is what the vendored fixture carries and what
the stage-5 smoke compares against. The bare short SHA a tagless clone used to record is
no longer accepted; a git-describe value stays accepted for a source pinned by commit.

`REQUESTABLE_SERVICE_MODULES` in `shared/contracts/dto/project.py` is the
new-project boundary and matches the kit's `copier.yml`: only `backend` and
`tg_bot` may enter `ProjectCreate` or the PO project tool. `ServiceModule` also
retains `notifications` and `frontend` solely to deserialize project values
written by the released `service-template` producer. Those legacy values remain
available to historical reads, cleanup, and port-role logic but are not offered,
substituted, or accepted for a new scaffold.

## Installing a kit package into a generated product

An explicit catalog selection on an existing compatible backend,tg_bot product uses
`plan_install(name)`, not an engineering recipe. The deterministic tool takes the
catalog snapshot injected into that planning attempt, selects the admitted package,
curated recommended library releases and released default binding, validates their
identities and binding function requirements, and creates one `TaskType.INSTALL`.
The pinned kit's `load_binding` selects the v1/v2 model and `validate_binding`
checks it against the released manifest. V2 has no parser functions; its closure
keeps an empty `binding.functions`. Unknown versions are `invalid_binding` refusals.
No model runs in closure planning or execution. `scripted_install_plan` invokes the
same tool, API, planning attempt, requirement coverage and admission without a model.
`create_task` refuses kit install prose; unrelated feature tasks retain ordinary chaining.

`dto/catalog_install.py` defines package/library name, distribution, version and
independent `packages/<name>/v<version>` tag; binding owner, module resource,
SHA256 and required library functions; core/Python admission, catalog digest and
resolved tooling SHA. Unknown fields, commands, supplied artifacts, traversal,
duplicate components and binding owners outside the closure refuse. TaskCreate
requires story/repository ownership; TaskRead/TaskDTO persist the same payload.
`tasks.install_operation` is API-owned; clients cannot forge it or move install
ownership through TaskUpdate. The migration adds nullable JSON columns and a DB
constraint pairing the payload with INSTALL and required ownership.

The reader reads only the activated catalog snapshot (`shared/catalog_activation.yaml`):
`packages/catalog.yaml` at its commit, verified against its raw SHA256 and the semantic
digest of the parsed catalog, on a host whose kit core and tooling are the activation's.
Other bytes are `provenance` and another host is `inactive`, both terminal for a stored
plan; a transport failure retries. It uses the pinned kit loader, bounded HTTP reads and a
five-minute successful-read cache; binding and manifest bytes come from the independent
released component tag. Every payload it plans names the catalog it was planned from
(`CatalogInstall.catalog`: repository, full commit, raw SHA256); a read at a moving ref
plans nothing (`catalog_unpinned`). Unavailable or invalid catalogs, unknown/incompatible
packages, missing recommendations/resources and binding dependency failures are named
refusals with no install task. The released reminders closure is reminders `0.5.0`,
recommended textparse `0.1.0`, and its default binding requiring `textparse.when`; the
executor rechecks it against the payload's catalog commit. API admission refuses a stored
payload without one (`catalog_unpinned`) before any operation exists.

Scheduler dispatch calls `POST /tasks/{id}/catalog-install` with `InstallCommand`.
Admission locks Task rows in order, then Story, Project, Repository and engineering
Runs. Coverage, predecessor, active prepared owned project, current story cycle,
stop/publication disposition and live branch writer fences precede a queued durable
operation. ScaffoldMessage mode=install carries its project/task/story/repository/cycle,
operation ID and validated payload. Redis NX/TTL throttles publication only; lost
responses republish the same operation. Concurrent claim tokens admit one writer;
response-loss replay uses its identical token. Paid admission and manual spawn refuse
`catalog_install_not_engineering` before executor selection, Run/attempt ledger,
budget reservation or worker publication. Ordinary branch writers also refuse while
an install is queued, running or requires recovery.

Scaffolder owns its existing project execution/teardown lease and GitHub App client,
a durable install heartbeat, and a nonblocking workspace lock. It requires the real
owned repository and clean checkout, refuses rejection artifacts even when ignored,
and switches or creates `story/<story_id>` without resetting unrelated content.
Unpublished local or changed remote heads refuse. Before package mutation, a fixed
read-only probe runs under the product's isolated interpreter and checks saved
source/ref, root requirement/lock/installed tooling, actual core, required modules,
independent tags, target Python, binding ownership and command/settings conflicts.
Older cores require a reviewed native Copier update; no overwrite or automatic repair.
Infrastructure Git uses process-local `core.hooksPath=/dev/null`, preserving the
product's local hook configuration. Only owned fetch, remote readback and push receive
repository-scoped Git authorization. Product probes, kit/component fetches, generation,
tests and per-service mypy use an explicit anonymous environment allowlist.

The executor runs fixed argument vectors in the product's own environment:
`kit add <package>`, `kit add <library>` for each recommendation, both with
`--catalog-source <repository> --catalog-ref <commit>` from the payload,
`kit bind <package> --default`, regeneration, spec validation, each service's own
mypy and existing product tests. Kit commands and the probe read the catalog at the
payload's commit and the independent released tags, never the kit's default branch;
no wheel/source override or task-authored patch is accepted. Readback
checks installed distributions/interpreter prefixes, default resource bytes,
backend allowlist and generated ACTIVE_PACKAGES manifest digests. Existing app,
controller, handler, owned tg_bot/src application files (excluding generated output),
spec, binding and environment bytes plus answers/root lock are
hashed before mutation and compared before commit. Binding v1 declares a timezone;
v2 declares a language and optionally a timezone. Setting keys and schemas come from
the binding through the kit's `binding_settings`, including probe conflict checks.
Confirmed explicit values use Product Brief initial_settings or the capability answers of
the brief's stored plan, through the existing seed/deploy path; missing confirmed values
are returned, never guessed.
Credentials, user IDs and timezone values never enter
parser/generator code.

Verification and exact base/head checkpoints precede a non-force commit/push. A
lost push response succeeds only after the remote exact head is observed. Publication
marks the Task done with verified closure under the same API fences. Existing story
completion resolves the exact branch/PR, checks that it contains the saved install
head (exact equality for install-only stories), and hands off to the existing CI,
merge and deploy owners. Scaffolder never opens duplicate PRs, merges or deploys.
CI, conflict, deploy and QA coding failures park mechanical work for review instead
of buying an engineering fallback.

A refusal records its finite stage and redacted bounded diagnostic. Preflight refusal
is `refused`; work after mutation, lease loss, cancellation or uncertain push is
`recovery_required`, retaining exact head and proof. Only its Story is parked;
cancelled Tasks and unrelated/newer stops are preserved. Expired leases are observed
by the scheduler, including cancelled running installs. Queue redelivery reads terminal
operations and never executes or commits again. Owned subprocess groups, heartbeat,
workspace lock, GitHub pool and project lease are released on all exits.
Workspace GC takes the same nonblocking repository flock before preservation checks,
deletion and API notification. A busy install is protected even without worker metadata.
The `.catalog-install-locks` directory and lock files are never collected or unlinked:
install releases after its subprocesses end; GC releases after cleanup notification.

`POST /tasks/{id}/catalog-install/recovery` requires an authenticated bearer admin,
selected current operation and matching stop/cause. `recover` requires the retained
verified head already published on the exact story branch; it performs no kit commands
or push. `retry` archives prior evidence and returns a reviewed repaired checkout's
Task to TODO. The operator must first clean/reconcile retained files and align the
local story branch with its reviewed remote; no executor reset is implicit. A cancelled
Task remains cancelled when retry releases its reviewed blocking operation. Wrong
cycle/operation, unrelated stop or unpublished/unverified head refuses.

`replan` answers the scheduler's park after a published install's PR failed CI. It
requires the `done` Task's current `published` operation, the scheduler's
`Catalog installation requires review:` stop with its unreleased stop ID, and an absent
remote `story/<id>` branch (`install_branch_present` otherwise). In one transaction it
releases the stop, cancels the Task and moves the Story `waiting_human_review` →
`failed` → `reopened`; after commit it publishes a reopen `ArchitectMessage`, so the
Architect plans a new install against the current catalog. A lost publish is answered
`architect_publish_failed` and re-sent with `send-to-architect`. It writes nothing to
GitHub; see [the operator runbook](../runbooks/catalog-install-replan.md).

A second `replan` shape names the install Task a previous replan cancelled, while the
Story is `waiting_human_review` with an unreleased stop and its `reopened_at` is still the
one that replan stamped (`replan_reopened`). It refuses a live run, an INSTALL or `done`
Task in the current cycle and a present remote branch; otherwise it releases the stop,
cancels the cycle's open Tasks, notes the install Task and reopens the Story the same way.
The Architect plans a reopen that a replan stamped without the LLM: one `plan_install`
per package of the cancelled install, against the catalog read now, and no other Task.
A Story with an INSTALL Task in its history is never planned while the catalog is
unavailable; it records a retriable planning failure and the scheduled retry plans it.

Expiry also settles an older cycle's writer without parking the newer Story. After
reconciling its retained checkout, bearer-admin `retry` can release that cancelled
operation alone with no stop ID; it preserves the current Story/cycle/quarantine,
retained proof and head. It cannot publish cancelled work or release another stop.

The existing template compatibility CI lane executes the production executor over a
real released notes product with hooks enabled before setup, preserves registered notes
save/list commands and protected application hashes, rejects a plain push with a failing
pre-push canary, and proves executor push/readback never invokes that canary. It
reads component tag object/target/tree provenance, and runs the released fake-backend
confirmation/preset/list/cancel corpus under the product bot interpreter. Its redacted
`mechanical-install-result.json` identifies the candidate, executed stages, readback
and exact non-force Git head. DB/Redis service tests prove actual persisted closure,
coverage, dispatch, zero engineering accounting and exclusive/recoverable ownership.
The older `mega-noop` engineering runner directive remains historical evidence of a
different route, retained by the ordinary engineering fixture and `mega-live`.
Registered `mega-noop` now selects `TestMechanicalInstall`: its second confirmed
brief transfers the PO-owned claim, taken before Architect publication, to the fixed
`python -m src.scripted_install_plan` invocation. The catalog reader and native
coverage/admission remain authoritative. Fixed QA borrows the scheduler's exact
Telegram grant, observes receipt, due arrival and preset cancellation in the real
chat, and leaves revocation to the existing owner. Its redacted
`mechanical-install-<run_id>.json` records partial phases, native operation/publication,
deployed registry digests, protected notes, timezone, accounting and chat correlation.
Its third confirmed brief selects an install by catalog capability and proves the deployed
manifest's platform sources against the internal stand fake's product, grant and key readback.
Readback collects every backend `*env.contract.yaml` fragment, including the generated packages
fragment, and validates and merges them with the same shared helper as deploy.
The stand-only `Stand conversation: <fixture>` checklist line selects finite chat test data
mounted into QA; admission and QA share its selector, and ordinary production admission
does not treat it as deterministic. The runner supports sends, visible callbacks and passive
message waits, writes language through the existing settings client and retains redacted RU/EN
results inside the same native QA grant. The reminders probe remains unchanged.
Only dispatcher-owned final-main stand evidence can establish live acceptance.
The persisted planning Python value is a compatibility baseline, not an observed product
interpreter; native preflight validates the actual product interpreter before mutation.

## Central QA of a product that carries a kit package

Central QA establishes a deployment's packages from the deployment itself, before an executor
exists. `services/langgraph/src/agents/qa/packages.py` reads three of the product's own artifacts
from `/app` in the one running backend container attributed to the deployment by its Docker
Compose project and service labels — the backend manifest's `packages:` allowlist,
`codegen_kit/_active_packages.py` with each package's name, version and manifest digest, and the
generated job registry `services/backend/src/generated/jobs_schemas.py`, which attributes every
fireable job to the service or the `package:<name>` that declared it — and
`run_package_activation_checks` cross-checks them against each other.

This is a fixed read boundary, not container exec access. The target's root-owned `qa-docker`
wrapper accepts only those three relative paths, resolves them below `/app`, requires a readable
regular file, and refuses a different byte limit. The caller may name only a container already in
the run's Compose-project capability set and selects exactly one running `backend` service.
Absence is the wrapper's distinct `5` result. No backend, two backends, a stopped backend, an
outside or refused path, Docker failure, unreadability, and a contract over 262144 bytes are read
failures, never evidence of a package-free product.

**An active package makes a run owe results, not prose.** A run against a product with an active
package owes one row for the package itself and one for each behaviour of it the run's criteria
declared, and they are rows in the run's result whatever the executor submits:

1. *The package is active in the deployed product.* The check is the package's `startup`, which
   raises on failure; the runtime refuses a generated contract that no longer matches the manifest
   and the installed wheels, and the container probe has already found the deployment up. A booted
   product carrying the package in that contract has passed it, so reading the contract off the
   live deployment performs the check rather than describing it.
2. *Each declared behaviour of the package produced its observable* — one row per behaviour, so
   two declared behaviours are two results and neither can go missing. Three things are required
   for that exact name, and each missing one is its own failure reason quoting the observable: the
   product accepted a **fire** of it; the run then made a **successful read of the route the
   criterion's observable names** — a read that answered, since an error response is the product
   answering about the request rather than about itself; and the run's result carries a **passing
   check that names the behaviour and quotes that read**. A criterion whose observable names no
   route on the deployed product names nothing this run can read, so nothing can be bound to it and
   the row fails saying that — it does not pass on an unrelated read, and it does not pass on
   absence. Neither the fire's acknowledgement nor a `job_evidence` read
   is that output: both answer with the product core's record of the dispatch, which says nothing
   about whether any provider consumed the event. A `job_evidence` read this run happened to make
   is not a requirement, but an `undelivered` command in it settles the row against the behaviour,
   because an event that never left the core cannot have been consumed.

**What this establishes, and what it does not.** The observable is prose an architect wrote — "GET
/reminders shows the reminder as emitted" — and no runner-side rule reads English. The
division of labour is therefore stated rather than fudged: *the executor judges the observable*,
and *the platform establishes that the work was done and that the executor judged it*, refusing a
verdict that rests on nothing the product did. A passing row says which read it is bound to and
which submitted check rests on that read; it never claims the words of the observable were
mechanically proven. An unrelated path cannot stand in for the route the observable names, and an
observable that names no route is not accepted on any read at all. Two consequences follow, and
they are the point rather than side effects: a criterion that wants a package behaviour accepted
must name an observable the run can read, and a run that cannot bind one is honestly red rather
than falsely green.

**One predicate decides it, and only an HTTP route binds.** Whether a recorded read answers a
criterion's observable is decided in exactly one place, `observation_answers` in
`services/langgraph/src/agents/qa/packages.py`, and exactly one caller turns that into a verdict,
`_behaviour_row` in `_qa_runner.py`; no other path writes a package behaviour row. A read answers
only when all three hold: the tool is an HTTP read of the deployed product (`http_get` or
`localhost_http_get`); the read genuinely succeeded, judged from the final status marker `curl`
itself wrote — the marker is read anchored to the end of the output, so a response body carrying
the text of a `200` marker cannot speak for `curl` and a failing route stays failed; and its path
matches a route the observable names.

**Central QA reads package routes as a verified QA user.** Kit core `2.1.0` takes a package
route's owner from the verified caller, so `GET /reminders` names no `user_ref` and an anonymous
read is 401. A run against a deployment that stores a `USER_IDENTITY_CAPABILITY` therefore has
exactly one QA identity, chosen once in `resolve_qa_caller_identity`
(`services/langgraph/src/agents/qa/caller_identity.py`) after the bot preflight and before any
executor starts:

- a run testing a bot reads as the QA Telegram account the run already proved and the bot already
  admitted, `telegram:<QA_TEST_TELEGRAM_ID>`, so what the executor creates through the bot as that
  account is what it reads back;
- any other run reads as the platform QA identity, channel `qa`, external id `central-qa`
  (`qa:central-qa`);
- a deployment with no stored `USER_IDENTITY_CAPABILITY` (a product older than kit `0.7.0`) gets no
  identity and its run is unchanged.

The runtime makes that identity active with `POST /users/grant` under the deployment's stored
`USERS_GRANT_CAPABILITY` and requires `GET /users/access` to report it `active`. A grant that is
not proved — no stored grant capability, a refused grant, an unreachable or malformed access read,
an inactive user — blocks the run as `qa_access_grant_failed` before an executor starts, with the
bounded failure kind as the reason; QA never falls back to anonymous reads. The run's metadata
records `qa_caller_identity` (`user_ref` and `active`), and the run's facts tell the executor which
`user_ref` its reads act as and that no request names a `user_ref` itself.

**Health-only QA holds the same identity.** Criteria that are all plain GET expectations
(`parse_health_only_criteria`) are decided by `run_health_checks` with no executor, and that leg is
built from the same two pieces as the exploratory one, in `consumers/qa.py`: `_run_secrets` reads
the project's stored secrets once and builds the run's `QARunRedaction` from them, and
`_establish_caller_identity` resolves the identity through `resolve_qa_caller_identity`, records
`qa_caller_identity` on the Run, and returns the same typed `qa_access_grant_failed` blocker for a
grant that is not proved. The health-only leg talks to no bot, so it always reads as the platform
QA identity `qa:central-qa`, even for a bot product. With an identity, every health GET carries the
three caller-identity headers, so `- GET /reminders returns 200` passes only for a verified caller
on a migrated schema (anonymous, it is 401); a deployment with no stored
`USER_IDENTITY_CAPABILITY` gets anonymous GETs exactly as before. Each check's detail — kept in the
Run's report and failed checks — ends with `; body: <snippet>` of what the path answered: the body
is scrubbed with the run's `QARunRedaction` first, its whitespace collapsed, and then cut to
`HEALTH_CHECK_BODY_MAX_CHARS` (500), so the evidence says what a route returned and never a
capability.

Only the runtime-side `http_get` of the deployed URL carries the identity: it sends exactly one
`X-Identity-Capability` (the stored `USER_IDENTITY_CAPABILITY`), one `X-User-Channel` and one
`X-User-External-Id` on every request when the run has an identity, and none of them otherwise.
`localhost_http_get` is a curl on the target and stays anonymous, because the capability must never
reach the target's argument vector; an identity-bearing package route is read with `http_get`, and
the platform facts say so. The same secret rule as the jobs capability applies to both the
identity capability and the grant capability: they exist only in runtime memory and as request
headers, never in a URL, a recorded request or response, an observation, the trace, an error or
log message, a verdict, the executor's environment or the `qa` CLI arguments. The grant client is
constructed only in `resolve_qa_caller_identity`, and its failures are a closed set of kinds. A
product can still reflect a capability back, in a body, a log line or an error, and the executor
can print the Telegram credentials or the run token it holds. So the run keeps **one** runtime-only
redaction set (`QARunRedaction`, `services/langgraph/src/consumers/_qa_redaction.py`) of every
secret its runtime handles. Each secret enters it once, where it enters the run:
`USER_IDENTITY_CAPABILITY`, `USERS_GRANT_CAPABILITY`, `JOBS_FIRE_CAPABILITY` and
`SETTINGS_WRITE_CAPABILITY` where `consumers/qa.py`
reads the project's secrets; the handed-over QA Telegram credentials where the runtime enters
`run_qa_centrally`; the endpoint's run token (`QA_CAPABILITY_TOKEN`) where `QACapabilityService`
mints it; and any value a call presents where `build_qa_callables` builds the calls. Every scrub
reads that same object, and no code path passes a separate tuple of secrets. It is applied at two
boundaries. At the **executor boundary**, `build_qa_callables` wraps every call it builds, so each
result is scrubbed before the executor receives it, including any call added later, and
`QACapabilityService._dispatch` scrubs probe input. At the **retention boundary**, `QAWorkspace`
scrubs everything it keeps (trace, observations, Telegram and probe evidence, report, verdict),
the runner scrubs the executor's report, verdict and transcript, and `run_qa_centrally` scrubs
its result on every exit path. The scrub runs before any bound. A text already cut by someone
else has a trailing fragment of a value redacted too. The live
`mega-brief-package` variant seeds `reminders.reminder_owner_ref` as `qa:central-qa`, the identity
its bot-less run reads as, so the seeded reminder is the one `GET /reminders` lists.

**A bot-only observable is not currently bindable, and that is accepted.** A package behaviour
criterion is accepted only when its observable names a route on the deployed product — for the
reminders package, `GET /reminders` showing the reminder in state `emitted` after
`reminders.tick`. An observable phrased as bot delivery — "THEN the bot sends the reminder text to
its owner" — names no such route, so nothing binds to it and its row fails saying the criterion
named no observable this run could read, even though this run's `telegram_probe` may have recorded
a bot reply as product output for every other purpose. This is a known accepted limitation of the
package path, not an oversight: admitting a probe read back into the binder would restore "any
post-fire observation answers", the hole this path exists to close, and a trustworthy non-HTTP
observation-target contract is a change to the QA acceptance contract that is deferred rather than
attempted. Criteria for a package behaviour are therefore written against a route, and the criterion
examples and the `ScheduledBehaviourCriterion` and `ProductObservation` docstrings say so.

`apply_package_acceptance` puts those rows in front of the executor's own checks and fails the run
when any of them failed, so a verdict that performed no package check does not pass by asserting
that it did. What this run fired, read back and read is decided from the runner's own ledgers —
`QAWorkspace.fired_behaviours`, `behaviour_evidence` and `observations`, all written by the runtime
when the product answered — never from an executor's account of itself.

**No prefixed-route check is required, and no prefix is inferred.** Package protocol v1 keeps a
package's `http.prefix` in the installed `package.yaml` inside the wheel, and the kit's generated
active-package contract records only `name`, `version` and `manifest_sha256` — so a deployed
product never tells QA where its package is mounted, and the prefix cannot be recovered from the
package's name, which the protocol keeps independent of it. QA therefore records that it could not
determine the route and claims nothing about it: a healthy package is not failed over a fact the
product never published. A criterion that names a package route is checked as the ordinary
criterion it is. Having the kit publish `http_prefix` in that generated contract would make a
deterministic route probe possible; it is recorded as deferred rather than taken.

The rest of the kit's package acceptance procedure — install the real wheel, resolve the entry
point, start the generated application, observe lifecycle calls, validate the manifest, run the
import lint — is deliberately **not** here. Those are build-time proofs, performed where the
product is built: in the kit's own CI and in the install recipe proof above, which runs them
against a real render. Central QA meets an already-deployed product and is read-only apart from the
one named fire, so asking it to install or lint anything would break that boundary and prove
nothing the build has not already proven.

A package's scheduled behaviour needs no mechanism of its own. The name is read off the run's
acceptance criteria by `parse_scheduled_behaviours`, retained by `prepare_central_qa_criteria`,
named to the executor by `scheduled_behaviour_facts` and accepted by `fire_job`, exactly as a
service's is; a package prefix such as `reminders.tick` is a name, and the rule that a dispatch
record is never the answer to the criterion's observable applies to it unchanged. What the deployed
product's own registry attributes to a package is stated as a fact, and a criteria-named behaviour
the product declares nowhere is named as a check that fails.

Nothing is inferred from an absent read. A deployment with no `packages:` key and no generated
package contract carries no package contract at all, and its run is unchanged: same criteria
preparation, same facts, same verdict shape. Every other disagreement fails the run before an
executor starts — a listed package with no generated contract, a generated set that disagrees with
the allowlist, an artifact that is truncated or unparseable, a read the target refused. A package
contract that could not be established is never reported as "no packages", because a check with
nothing to examine has to fail. The reader is proved against a real render rather than a replica:
the stage-5 template compatibility smoke runs it over the artifacts `kit add reminders` generated
in the product it just rendered.

**The live proof of this path is the `mega-brief-package` suite.** It is the confirmed Product
Brief flow — `tests/live/brief_pipeline.py`, shared with `mega-brief` — run on a second product
contract whose must-requirement is a one-time reminder: a self-contained capability with its own
storage, its own routes under one prefix and its own scheduled behaviour, which is the shape the
architect's capability ladder resolves to an in-process kit package. So one paid run exercises the
whole package route: the architect plans the capability as a package, the engineering worker
installs it with the kit recipe above, and central QA judges the package behaviour under the rules
of this section. Because those rules bind a behaviour row only to a read of a route the criterion's
observable *names*, the variant's product contract asks for exactly one criterion line —
`FIRE JOB reminders.tick WITH {"at": …} THEN GET /reminders read as qa:central-qa shows the seeded
reminder in state emitted` — and the harness refuses the published criterion, before the run is
paid for, when `observation_answers` cannot bind a `/reminders` read to it. The variant's expected
behaviour shape is its own: `reminders.tick` carries the `at` its declared `jobs_schema` requires,
where the digest variant's behaviour takes no arguments. `scripts/stand_run.py` names the suite,
and it runs under a longer productive window than `mega-brief` because its engineering turn pays
for the kit install first (`shared/stand_deadlines.py`).

**And the variant establishes the package route from the deployment, or it is red.** The capability
ladder puts a shared service *above* the package option, so an architect may choose one and a worker
may hand-write a `reminders.tick` job and a `/reminders` route. That product satisfies everything
this suite reads off the control plane, and central QA finds no activation on it, writes no package
behaviour row, and passes it through the ordinary fire-and-read path — a green run that proves
nothing about packages. So before a QA turn is spent, the variant resolves the same running backend
container and reads the deployment's generated artifacts below its `/app`, the ones this section's
checks read (`codegen_kit/_active_packages.py` and
`services/backend/src/generated/jobs_schemas.py`), with the same parsers, and requires two facts:
the product records `reminders` as an active package, and its own registry attributes the fired
behaviour to that package rather than to a service of its own. Anything else ends the run with the
reason, and an artifact that could not be read is one of those reasons rather than a skip. This is
an assertion in the variant's harness about what this run must have produced
(`tests/live/package_route.py`), not a new rule in the runner. The host needs only its production
Compose files; no product checkout is inspected. A package-free product's QA path is unchanged. A
run whose architect chose another permitted shape is a run that failed to demonstrate the package
route, and it says so rather than being prevented from happening.

## A QA run keeps the executor's own transcript

`QARunResult.executor_transcript` carries what the QA executor said, as the QA
runner saw it over the worker's output stream (`QAExecutorRun.transcript`,
bounded there). It is on the Run because nowhere else survives: a QA executor
container writes no transcript under the worker-transcript mount, so once the
stand is destroyed the paid run's acceptance artifact could only report the
absence — which is what run 34055029359 did. `tests/live/run_evidence.py`
retains the value as the QA worker's `transcript.content`, redacted and bounded
through the one retention funnel.

**Every attempt that ran is kept.** QA retries a transient failure to start its
executor, and an attempt that ran and said something must not be erased by a
later one that never started a container. The runner collects the attempts
(`QAExecutorAttempts`) and writes those that produced output under one header
each — `== QA executor attempt N of M ==` — so a reader can tell them apart.
That applies to the answered run as much as to the failed one: an attempt that
spoke before a successful retry is evidence too. The header is presentation and
only ever goes around output an executor produced; assembled text is never
published as content.

**The field has three states and they never merge:**

* **a non-empty string — retained.** An executor produced output and the
  artifact carries it, redacted and bounded through the one retention funnel.
* **`""` — known silent.** At least one executor attempt ran and none of them
  said anything. Only the QA runner writes this, and it writes it from its own
  observation, so the artifact states an absence that names the executor's
  silence. The sweep race below does not touch it: an empty transcript is a fact
  somebody watched, not the default of a writer that had nothing.
* **`null` or the field absent — not recorded.** The writer that settled the Run
  had no transcript to record. It says this writer recorded none and nothing
  more; it may not be read as "no executor produced output", because more than
  one writer can settle a QA Run and only one of them ever holds that output:
  the QA consumer that ran the executor. Its own fallback terminal write settles
  the Run when the first PATCH fails for anything but a 409, and the QA grant
  sweep and the temporary-access sweep can settle a Run — through the 409
  refusal and `record_run_outcome_unless_settled` — while an executor is still
  in flight and its output exists only in the runner's call stack. The artifact
  says which writer settled the Run without a transcript, as the Run itself
  records it, and asserts nothing further.

That the sweeps can settle an in-flight Run is a known residual and stays one
for this sprint: closing it would change terminal ownership and the run
lifecycle. Under the rule above it costs no false statement — the artifact
reports which path settled the Run and claims nothing about the executor.

## QA probes are Run evidence

Telegram message and callback tools accept an integer collection wait of 1–60 seconds,
default 15; their child process timeout is that wait plus 30 seconds. Invalid waits
return a typed `invalid_wait` refusal without delivery or a run blocker.
At the single runner settlement point, an executor-declared `product` cause is
always preserved. Settlement moves only failed `qa_capability` / `qa_tooling`
rows to unverified; brief text and optional `telegram_step` evidence never
rewrite a product cause. The executor uses the brief, its examples and visible
bot help/commands to decide the input contract. Unsupported exploratory inputs
are reported as `qa_capability`, or `qa_tooling` with cited evidence.
The executor may classify a missed reply as `qa_tooling` when its detail cites
concrete server-side evidence that this interaction received an answer. Settlement
retains that citation as unverified, at the same trust level as `qa_capability`;
owner verification facts say "QA tooling", and no engineering fix is created.
There is no automatic server-log override: the kit send-log contract is deferred
to issue:8156ad9d43c12dc956f9. The additive cause and nullable probe message id
preserve historical result reads.


`QARunResult.probe_runs` retains each `qa probe` record in capability-call
order: its runner-assigned id, closed platform (`telegram`, `http`, or `web`),
source, arguments, stdout, stderr, exit status, duration and per-text truncation
flags. The capability endpoint bounds every text field and the per-Run count,
scrubs the QA Telethon credentials and run token before storage, and refuses a
malformed record with an error response. `[]` means the executor ran and no
probes were recorded; `null` means the terminal writer held no probe record and
does not claim anything about an executor it did not observe.

Probe source and output enter the same forbidden-application-write scan as the
runner trace, report, verdict and transcript. A hidden POST, PUT, PATCH or
DELETE therefore fails the Run closed even when its executor verdict says pass.

`qa probe` cuts each of source, stdout and stderr at 19,000 characters *and* at
64,000 bytes as JSON-encoded on the wire (a C0 control character is six bytes,
`\u0001`), so a record is always under the endpoint's 256 KiB body limit; the
record carries `file_kind` (`py` or `sh`), `null` on records that predate it.

QA executor containers retain no transcript file. Their output is retained only
as `executor_transcript` and `probe_runs`; QA Run, attempt and executor-result
records omit a transcript locator, and qa-worker deletes `worker:{id}:output`
when the run ends so a session cannot survive in the broker stream.

## The QA capability catalogue

What central QA can do is declared once, in `shared/contracts/qa_capabilities.py`.
`QA_ACTIONS` are typed entries: platform (`telegram`, `http`, `web`, `job`), action,
route (`tool` — a fixed `qa` call; `sandbox_probe` — a script the executor writes and
runs through `qa probe`; `library_seed` — a ready probe under `/workspace/qa-library`)
and one line of prompt wording; `criterion: false` marks an executor-only read that no
criterion is written through. Telegram offers what a user account sends through Telethon:
text (`telegram_probe`), an inline button (`telegram_click_button`), a location (the
`telegram/location` seed), and a contact, photo/file/media, reply or edit through a
probe. `QA_NEVER` declares, each with its reason, what QA never performs: an HTTP POST,
PUT, PATCH or DELETE to the product's API (policy), a direct read or write of the
product's stored data or state, and anything outside the run's deployment and Telegram.

A sandbox platform is offered only while the `qa_sandbox` image capability installs its
tooling: `QA_SANDBOX_TOOLING` names the package (`telegram` → `telethon`, `web` →
`playwright`, not installed), a worker-manager unit test pins it against
`CAPABILITY_INSTALL_MAP["QA_SANDBOX"]`, and `qa_actions()` is the one filter. Every
`tool` call is on `QA_PROBE_USAGE`, and the CLI's other calls are `QA_RUNTIME_CALLS`.

Consumers read the catalogue, never a list of their own: the Architect's "What QA Can
Check", the PO `present_product_brief` must-requirement guidance (rendered into the
docstring before `@tool` reads it) and the QA executor's "What you can check" come from
one renderer each in `services/langgraph/src/prompts/qa_capabilities.py`; the pre-QA
filter takes its withheld HTTP methods from `http_write_methods()`. A tripwire test
renders each consumer's text and fails when a catalogue wording or a retired phrase
appears outside the generated block.

**A must-requirement QA cannot check is settled at planning time.** The Architect's
"What QA Can Check" ends with the rule, rendered beside the catalogue's lists: when the
only usage example of a must-requirement (or every one) needs an action the catalogue
does not name or lists under never, the Architect rewrites the check into an observable
QA can check, or returns the requirement with
`record_requirement_coverage(requirement_id=..., returned_reason=...)` whose reason starts
with `NOT_AUTOMATICALLY_VERIFIABLE_PREFIX` (`"not automatically verifiable:"`,
`shared/contracts/dto/product_brief.py`) followed by what QA would need. Such a return is
an ordinary return: the admitted plan's `story_requirements_returned` carries the reason
to PO like any other.

## The QA probe library

A project's library (`qa_probes`) is unique on project, platform and name and
holds at most `QA_PROBE_LIBRARY_CAP` (50) entries; a store past the cap evicts
the oldest `updated_at`. It is filled only after the QA consumer's own PASSED
write settled the Run: `POST /projects/{id}/qa-probes/from-run` validates that
Run (this project, type `qa`, `completed`, `qa_outcome: passed`) and upserts every
`probe_runs` record with exit status 0, an untruncated source and a known
`file_kind` — the last record of a repeated name wins. It is the one writer, so
it alone decides which names exist: a record whose name does not match
`QA_PROBE_LIBRARY_NAME_PATTERN` (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) is skipped,
never rewritten, and counted in the response's `skipped`. Its source is the record
the capability endpoint already scrubbed and bounded; no other path writes a
library entry. A FAIL, BLOCKED, EXHAUSTED or infrastructure outcome stores
nothing, and a failed library write is logged and changes neither the verdict nor
the Run.

At run start the QA consumer reads the project's entries and the platform seeds
(`shared/qa_probe_library/`) and sends them as `WorkerConfig.qa_probe_library`;
worker-manager writes them under `/workspace/qa-library/<platform>/<name>.<py|sh>`
with `index.json` (name, platform, origin `seed` or the storing run id, file,
one-line usage) before the executor is marked running. The entry name is its
file stem, so the table's unique key makes paths unique, and
`QA_PROBE_LIBRARY_FILE_PATTERN` closes them. The index is bounded by arithmetic,
`QA_PROBE_LIBRARY_INDEX_MAX`: (cap + at most 13 seeds) rows of at most
256 + 4 × 64 (name) + 6 × 255 (a JSON-escaped `runs.id` origin) + 64 (usage
arguments) characters. Seeds are offered to a run with a `bot_username` (the
Telegram bot under test) and shadow a stored entry of the same platform and
name; another project's entry is never offered.

A stored library never makes a run fail. A failed library read, or stored
entries that cannot be laid out as one executor's library (a non-library name,
a repeated path, more than `QA_PROBE_LIBRARY_MAX_FILES` files, a file or index
over its bound — rows written before the name rule or by any other path), run
with the seeds alone; the seeds alone always build. `QARunResult.probe_library`
records the offer: `offered` (platform, name, origin), `read_failure` and
`build_failure` (a bounded note of why the stored entries were dropped). It is
set whenever `run_qa_centrally` returned, including a result that failed before
the executor started, and `null` only when the run ended before the library was
prepared (a preflight blocker) or the runner raised.

The Telegram location seed takes `BOT LAT LON [WAIT_SECONDS]`, parses the
coordinates with `float()`, refuses NaN, infinities and values outside
[-90, 90] / [-180, 180] before importing Telethon, and passes them to Telethon as
values; it generates no source. It connects with the `qa telegram_identity` file
through the run's proxy and exits 3 when that identity is missing or unproven.
