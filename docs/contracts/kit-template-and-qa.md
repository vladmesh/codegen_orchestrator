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
Production scaffolds from `gh:vladmesh/codegen-product-kit`, pinned by that
repository's release tag and no longer from `service-template`.
The production boundary is the annotated `0.6.4` tag, object
`2fe1dc027834d4118eff21af81dc942909aa7ebf`, which dereferences to
`04e2d94826f0dd6b46be3d7345b46cdd677db7ed`; the matching
`shared/tests/fixtures/codegen-product-kit-0.6.4` tree is its `backend,tg_bot`
Copier render and records that tag in `_commit`.
It represents the committed checkout: generated ignored `.env` and `TASK.md`
are omitted; every versioned rendered file retains the producer's bytes.
Its main-push image workflow runs frozen root sync, frozen `services/backend`
sync, and generation in that order before building either service image.
The render carries bigint user identifiers, forward migration `e6b8c2d4a901`
after `d4a7b2c9e1f0`, and Telegram token protection in HTTP logs. The kit's
[release proofs](https://github.com/vladmesh/codegen-product-kit/blob/f23460c62fa3508858c0552557b2860af09f2656/docs/releases/0.6.3.md)
cover PostgreSQL preserving upgrade/readback above int32 and real HTTP logging.
Existing Copier products retain owned ORM files: their later update must reconcile
`User.id`, `UserChannel.user_id`, and `Setting.subject_id` and apply the forward
migration. Downgrade refuses data or sequence values outside int32. This pin
changes new-product scaffolding; it does not migrate deployed products.
The generated deployment runs on `ubuntu-24.04`, keeps `DEPLOY_HOST` raw for
native SSH and appleboy/ssh-action, and brackets IPv6 only in the native SCP
remote destination. The same two compose files, target, options and three
attempts remain. Root tooling and its lock resolve the released commit; backend
and bot retain their own frozen service environments. Application/tooling
versions remain `0.1.0`; minimum Copier remains `9.0.0`.

Existing products need a reviewed Copier update on a clean review branch:
`copier update --defaults --trust --vcs-ref=0.6.4 --conflict=rej`. Preserve selected
modules and owned application/spec/environment bytes, back up ignored real
environment data through the product's restricted procedure, and compare its
bytes locally without exposing credentials. Read back answers/source, tooling
revision, both frozen environments and the full workflow. An exit of zero and
new answers can coexist with `.rej`: Copier installs the candidate workflow
while retaining rejected local hunks in the artifact and committed predecessor.
Reconcile each hunk deliberately, retain reviewed unrelated customizations,
resolve rejection artifacts and validate the product before its normal reviewed
merge and image publication. No bulk updater or remote workflow patch exists.
The immutable [release notes](https://github.com/vladmesh/codegen-product-kit/blob/04e2d94826f0dd6b46be3d7345b46cdd677db7ed/docs/releases/0.6.4.md)
and generated `infra/README.md` describe this boundary; the release document's
preparation heading predates publication, whose annotated identity above was
independently read back. External action execution, remote authentication and
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

A kit package is an installed wheel that declares a `codegen_kit.packages` entry point, and the
generated product activates one only when it is both installed and listed under `packages:` in
`services/backend/manifest.yaml`. Nothing publishes those wheels: `kit add <name> --wheel
<artifact>` deliberately takes an artifact path, because package publication and catalog
resolution are outside package protocol v1. The recipe an engineering worker follows therefore
builds the wheel from the kit source at the ref the product is pinned to, which the product itself
records in `.copier-answers.yml` (`_src_path`, `_commit`): obtain the kit at `_commit`, `uv build
--wheel packages/<distribution>`, then `kit add <name> --wheel <built wheel>` from the product
root. Nothing has to be vendored into a product that will never install a package.

`kit add` performs the whole product mutation — the wheel copy under
`services/backend/packages/`, the backend dependency and its lock entry, the entry-point-only
dependency record dependency linting needs, the manifest allowlist entry, `uv sync --frozen` of
the backend environment, and regeneration. Package code is never hand-written into a product.

Regeneration is part of installing or changing a package or a manifest, not an optional follow-up.
Generation writes the active package set with each package's manifest digest into
`codegen_kit/_active_packages.py`, and the runtime refuses a stale or changed generated contract
(`generated package contract is stale; run make generate-from-spec`), so a product whose manifest
was edited without regenerating does not boot.

The architect decides that a capability is a package in the first place. Its prompt
(`services/langgraph/src/prompts/architect/__init__.py`, "Capability Shape") states the ladder —
reuse what exists, then a shared service, then a container, then an in-process kit package — the
two shapes that disqualify a package outright (a capability needing a synchronous call into the
host, or a stateless consumer), and the protocol a package plan accepts: event-only outward
dependency, prefixed settings and job names, an owned schema and migration version table, and the
import boundary the lint enforces. A task it plans for a package asks for the install recipe above
by reference and never for hand-written package code. The decision is carried in the task's
description and acceptance criteria, not in a `TaskCreate` field: no tool argument names a
capability shape.

The orchestrator states this recipe to the engineering worker in
`services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`. The stage-5 template
compatibility smoke proves it against a real render rather than a replica: it renders a second
product from the same pinned ref, asserts that a product with no packages ships `packages: []` and
an empty `ACTIVE_PACKAGES`, installs `reminders` through `kit add`, and asserts the generated
contract then records the package's name, version and manifest digest.

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
/reminders?user_ref=42 shows the reminder as emitted" — and no runner-side rule reads English. The
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

**A bot-only observable is not currently bindable, and that is accepted.** A package behaviour
criterion is accepted only when its observable names a route on the deployed product — for the
reminders package, `GET /reminders?user_ref=42` showing the reminder in state `emitted` after
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
`FIRE JOB reminders.tick WITH {"at": …} THEN GET /reminders?user_ref=… shows that reference's
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
