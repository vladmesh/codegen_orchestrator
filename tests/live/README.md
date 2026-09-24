# Live harness contract

Pipeline tests create one `OwnershipManifest` per run. The manifest is written under
`.live-manifests/` and records exact project, GitHub repository, Redis entry, port allocation and
server deployment identifiers as they become known. Teardown addresses only those identifiers.
It never deletes a shared Redis stream and never matches resources by the historical `live-test-*`
prefix — matching by prefix belongs to the global `scripts/clean_live_tests.py` sweep, which owns
no manifest.

The deploy is the one identifier owned *before* the resource exists. The pipeline, not the harness,
starts the deploy run, so ownership that waited for a running application would arrive after the
stack. A run records its stack name — the project slug — when it creates its story, in
`create_story_and_task`: the story is what makes a deploy reachable at all, because the scheduler
opens a PR from `story/<id>` and `pr_poller` turns that merge into a deploy run without asking the
harness. Nothing is declared at the call site — a run owns the stack exactly when it does the thing
that can lead to a deploy, so a new live test that drives engineering owns it without knowing this
rule. A run that creates no story — scaffold — reaches no deploy and owns no stack, and never
touches a server on teardown. `wait_deploy` owns the same record again on entry (`own` merges, so
this never makes a second record) and then enriches it with the resolved server and port; under
uncertainty the harness owns rather than skips, because an over-owned record costs an SSH round
trip while an unowned one costs a live stack nobody knows about.

A record no target has been resolved for yet is cleared by its exact stack name on every server
`/api/servers/` lists: the manifest knows the name but not yet the host, and running that removal on
the wrong host removes nothing. An empty server list fails the teardown of an owned deploy rather
than passing it: it would prove nothing about a stack the manifest says may exist.

Cleanup is part of the test result. Every delete command must succeed and each owned resource must
then be observed as absent. A delete or verification error fails the run, including when the test
body already failed. The one exception is stated rather than tolerated: the database rows the schema
itself refuses to delete are declared retained, proven still there and reported — see **Database
teardown, derived from the catalog**.

Scaffold stream deletion is not treated as cancellation. Each execution atomically checks the
project cancel marker and registers its own expiring lease before external work. Concurrent or
reclaimed jobs therefore hold distinct tokens. Teardown writes the cancel marker and waits for all
leases to finish before external deletion and residue verification. Workers refresh live leases;
a crashed worker's lease expires and is pruned while teardown waits.

The repository root is derived from `tests/live/live_harness.py`. `ORCHESTRATOR_ROOT` may override
it, but the target must contain `pyproject.toml` and `tests/live`.

## Stand suite contract

The stand runner (`scripts/stand_run.py`) is the canonical contract for named E2E suites. A name
always identifies one pytest node, whether the run can spend model budget, its number of agent
combinations, and its subprocess timeout. The GitHub Actions dropdown exposes only the canonical
names. `mega` and `llm` remain temporary runner aliases for `mega-noop` and `mega-llm`; reports,
JUnit metadata, logs, and run directories always record the canonical name.

| Suite | Pytest target | LLM/model turns | Runs | Project / engineering / deploy / QA | Cleanup | Pytest cap | Expected duration |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `mega-noop` | `tests/live/test_full_pipeline.py::TestFullPipeline` | 0; three scripted engineering Tasks across two Stories and deterministic QA | 1 | a user registered through the product's own door — a fresh Telegram id, a promo code minted through the internal API and redeemed by that named actor, and the engineering budget policy the code arms; one `backend`+`tg_bot` product with its bot token bound through the product route; a Russian Product Brief confirmed through the released PO tools and its plan admitted through the architect's own coverage routes, both with no model call; paid admission evidence; two ordered scripted Tasks on one Story worker, each applying a change set; deploy with the confirmed settings seeded into the product; deterministic QA; completed Story/PO record and the bot product's own completion message; then a **second story on the same project** — a corrected brief revision confirmed through the same PO tools, one scripted Task on the reused workspace, deploy through the PR poller, QA, and a second completion message; explicit undeploy | manifest-owned, fail-closed, then product undeploy verifies port and bot-binding release | 155 min | measured from stand artifacts; no baseline measurement yet |
| `mega-llm` | `tests/live/test_full_pipeline.py::TestFullPipelineLLM` | one developer + one QA executor turn | 1 selected `--worker` / `--qa` pair | one project; selected developer; deploy; selected QA executor | manifest-owned, fail-closed | 60 min | measured from stand artifacts; no baseline measurement yet |
| `mega-brief` | `tests/live/test_product_brief_pipeline.py::TestProductBriefPipeline` | one Architect, developer and QA executor turn | 1 selected `--worker` / `--qa` pair | confirmed Product Brief; Architect coverage/admission; selected developer; deploy settings seed; selected QA executor | manifest-owned, fail-closed | 50 min + 10 min grace | the fixture's own productive deadline, then the runner's hard stop; no baseline measurement yet |
| `mega-brief-package` | `tests/live/test_product_brief_package_pipeline.py::TestProductBriefPackagePipeline` | one Architect, developer and QA executor turn | 1 selected `--worker` / `--qa` pair | confirmed Product Brief whose capability is a one-time reminder; Architect plans it as a kit package; the worker installs it with the kit recipe; deploy settings seed; the deployment's own package contract and job registry must show the capability is that package; central QA judges the package behaviour on the route its criterion names | manifest-owned, fail-closed | 65 min + 15 min grace | a longer productive window than `mega-brief`, because the kit install is inside its engineering budget; no baseline measurement yet |
| `matrix` | `tests/live/test_full_pipeline.py::TestFullPipelineLLM` | 8 total: developer + QA for each cell | 4: Claude/Codex QA × Claude/Codex developer | one complete LLM pipeline per cell | after every pytest cell and a final runner sweep, both fail-closed | 60 min per cell | measured from stand artifacts; no baseline measurement yet |

The local target names reflect that same contract:

- `make test-live-mega-noop` runs only the noop class.
- `make test-live-mega-llm` runs only the LLM class for one locally configured pair.
- `make test-live-mega-brief` runs only the Product Brief E2E class for one locally configured pair.
- `make test-live-mega-brief-package` runs only its package variant, the same path onto the kit
  package route, for one locally configured pair.
- `make test-live-matrix` delegates the four paid cells to the stand runner.

There is no compatibility alias or aggregate target in the local Makefile: select the named class
that owns the coverage you want, or use `make stand-run SUITE=<suite>` for a canonical stand run.

### Switching the QA executor

An LLM cell asks for a QA executor, and the runner has to make that true before pytest starts.
`QA_EXECUTOR_AGENT_TYPE` is written to the deployed `.env`, and every compose service the variable
is passed to is force-recreated — the set is read out of the compose files by
`stand_run.qa_executor_services`, never transcribed, so a service that starts reading it tomorrow is
recreated without anyone editing the runner. Today that set is `api` and `qa-worker`.

The recreate does not return until every service it recreated is usable the way the suite will use
it: `stand_run.recreate_and_wait` is the runner's only way to bring a container up — `_compose`
refuses `up`, `start` and `restart` anywhere else — so recreating and waiting cannot be separated by
a later caller. The stand has no locally built service image to recreate from, so every `_compose`
call adds the service release override bring-up generated (`STAND_SERVICE_RELEASE_COMPOSE`, set by
the workflow), the recreate runs `up --no-build --pull never`, verbs that could build or pull
(`build`, `create`, `run`, `pull`, `start`, `restart`) are refused, and a run without the override
is refused before preflight as `release_override_missing`. `api` is asked for `GET /health` on `http://localhost:8000`, the base URL
`conftest.py` builds every client on, because that is the fact the suite depends on; a consumer such
as `qa-worker` is ready when its container has logged `<service>_started`, the line
`run_queue_worker` prints once it is connected to Redis and reading its queue. The wait copies the
workflow's own policy — probe, sleep five seconds, give up at 180 (`stand-e2e.yml`, "Bring up
dynamic orchestrator and wait for API") — rather than inventing a second one; it cannot literally
share that loop, which is bash on the host, and it deliberately probes from outside the container
instead of `exec … curl 127.0.0.1`.

The switch is then confirmed by asking the resolver itself: `docker compose exec api` runs the same
`resolve_executor_decision` call paid-run admission makes, in the process whose settings are its
input, creating no Run. A consumer's local setting is not an acceptable confirmation — it flips the
moment that consumer is recreated and says nothing about the service that decides. Until the
resolver answers the requested executor the switch is unconfirmed, and a cell whose switch never
lands is reported `qa_executor_switch_failed` instead of running, and so is one whose stack never
became reachable: a timeout at either wait ends the cell rather than starting pytest against a stack
that is coming up. The database-backed break-glass
executor override outranks this variable by design and is not applied by the dry run: a stand under
an active QA override is not switchable from `.env` at all.

Run 33749154999 is why the wait is part of the recreate. The widened recreate set was correct, and
the resolver answered `claude` from the new `api` process as soon as its Python could import — some
seconds before uvicorn listened. pytest started at 11:30:08 and `ensure_test_user` died of an
`httpx.ReadError`; `api` logged `Application startup complete` at 11:30:12. A probe run inside the
container would have passed throughout.

Run 33743251165 is why all of this is written down. It recreated `qa-worker` alone and confirmed the
switch by reading that same container, so the `api` container kept its start-up `codex`, and a run
that asked for Claude spent Codex — silently, from 2026-08-27, until Codex hit its weekly limit and
could not log in.

### Timeout budget

The timeout values are deliberate bounds, not duration estimates. The scaffold bound is the one that
is not a constant: `shared/stand_deadlines.py` derives it from the product's module count — 120
seconds for the first rendered service and another 120 for each further one — because `make setup`
runs `uv sync --frozen` for the root and then for every service before `framework.generate` and ruff.
`mega-noop` scaffolds the two-module level-1 product, so its scaffold bound is 240 seconds; every
one-module suite keeps 120. The noop lifecycle's explicit waits are not transcribed here any more,
and they are not transcribed in `shared/stand_deadlines.py` either: the ledger there
(`NOOP_LIFECYCLE_WAITS`) is built from the wait constants themselves — `DEPLOY_RUN_TIMEOUT`,
`ENGINEERING_TIMEOUT`, `QA_RUN_TIMEOUT` and the rest, which `tests/live/pipeline_helpers.py` imports
from that same module — so a timeout that moves takes the ledger and the cap with it. The numbers
below are what it sums to today, not a second copy of it. The ledger sums to 140m40s and the
155-minute cap leaves 14m20s for manifest-owned teardown and diagnostics — at least the 11m40s
reserve the ledger requires of it, which is checked where both are defined.

It has three parts. The **first story** spends 68m20s (`240 + 840 + 60 + 1320 + 420 + 120 + 320 +
300 + 180 + 180 + 120` seconds): scaffold; two ordered noop Tasks; Story aggregation; the merged
deploy Run, whose 1320 seconds legitimately span the *product's own* CI, because no Run is created
until the merged commit's images are observed published; deploy; the typed deploy outcome; a bounded
public health probe (each attempt tries both health paths, each with the client timeout);
deterministic QA; completed-story and durable PO delivery; the exact deployment record. The **second
story on the same project** spends 62m20s (`420 + 60 + 1320 + 540 + 420 + 320 + 300 + 180 + 180`):
one noop Task and its Story aggregation; its own merged deploy Run, with the same image-publication
bound inside it; the typed deploy outcome, which for a second story has to cover the deploy itself,
because the application is already `running` from the first story's deploy and stays terminal
throughout a redeploy — the Run is the fact, not the status; the application's own terminal status
once that Run has settled; the health probe; QA; completed-story and PO delivery. **Teardown**
spends 10m: undeploy Run, terminal application and port-allocation release. The whole `mega-noop`
path — 45m provisioning, 10m pre-provisioning reserve, preflight, readiness, the executor switch,
this cap, the sweep and the job reserve — comes to 234 of the workflow's 360 job-minutes. The LLM pipeline
remains 53 minutes (`120 + 1800 + 420 + 420 + 120 + 300`) because its project is backend-only — one
module, so one module's scaffold bound — and it does not yet run the new lifecycle
acceptance. `mega-brief` stops its productive work at 50 minutes on the fixture's own clock and
then gets a 10-minute cleanup grace before the runner kills the process group — the two are
`MEGA_BRIEF_PRODUCTIVE_SECONDS` and `MEGA_BRIEF_HARD_STOP_SECONDS`, and the grace is their
difference, not a third number. `mega-brief-package` runs the same lifecycle under a longer
productive window — 65 minutes, then a 15-minute cleanup grace — because
its engineering turn obtains the kit, builds the package wheel, installs it with `kit add` and
regenerates the product contract before any of its own work starts. A recreate's readiness wait and the QA executor switch that follows it are separately
limited to three minutes each; runner preflight and final sweep are each five minutes.

For the largest workflow path, provisioning has a 45-minute budget. Its configured waits include
two 10-minute machine allocations, five minutes for DNS, three minutes for API readiness, and 20
minutes for target provisioning; the remainder is bootstrap/Ansible reserve. The previous broad
control-plane bootstrap measured about seven minutes. It now uses a stand-only minimal playbook
whose expected 2–3 minute duration is pending live confirmation; that expectation does not change
the overall provisioning budget. The matrix runner is bounded at 274 minutes (`5m preflight + 4 ×
(60m cell + 3m readiness + 3m switch) + 5m sweep`). The E2E job cap is 360 minutes, a strict
31-minute reserve over provisioning, the 10-minute pre-provisioning reserve and that runner path. Lifecycle cleanup runs in its own 30-minute GitHub job, because
jobs do not share an outer timeout.

### Invariant map and first-iteration baseline

All named suites exercise the product acceptance path: project creation, scaffold, engineering,
deploy, and QA verdict. `mega-noop` additionally proves each Task's admitted paid-run audit,
immutable noop `ExecutorDecision`, typed terminal result, canonical zero-provider-cost ledger row,
and actual reservation outcome; its second `todo` Task is blocked by the first and must not receive
a Run early. The two Tasks complete through one observed Story-worker lifecycle before the PR/merge
can lead to deploy. It also proves the completed Story's durable `story_completed` owner record, its
matching post-cursor PO input event and the message a *bot* product's owner is owed — the bot handle,
the confirmed brief's usage examples in the brief's language, and no backend address at all — the
successful service deployment's exact merged SHA, and a product API undeploy through terminal
`not_deployed` plus owned port-allocation absence. Its Story is planned against a confirmed Product
Brief the released PO tools froze without any model call, and released only by the one admission step
on the architect's own coverage routes — and because publishing that story wakes the live architect
consumer, the run proves from durable rows that nothing but the harness ever claimed this brief's
plan, rather than relying on having won that race; the grant deploy that follows seeds that brief's
`initial_settings` into the deployed product, and the run reads the value back from the product
itself. Every named suite also compares the deploy Run's image references with the commit `main` points at —
read from GitHub, never from what the deploy was given — before it spends a QA attempt, so a
deployment running an older image fails as a deploy defect rather than as a product one. Because no
deploy Run is created until that commit's images are published, `DEPLOY_RUN_TIMEOUT` now spans the
generated project's own CI while `DEPLOY_TIMEOUT` still means "deploy.yml + smoke". The LLM suites additionally prove selected executor wiring; they do not yet claim
the new lifecycle acceptance. The static baseline at this point is one two-Task noop run, one selected LLM
pair, or four unique matrix pairs; it does not claim unmeasured wall times.

| Invariant level | Primary evidence | Suites |
| --- | --- | --- |
| Product acceptance | `TestFullPipeline` / `TestFullPipelineLLM` status, deploy, health, and QA assertions | all named suites |
| Noop paid-work settlement | admitted audit, persisted decision, typed terminal Run, reservation readback, and ledger row | `mega-noop` |
| Ordered Story work | dependency-fenced second Task, one observed developer worker, and both Tasks done before deploy | `mega-noop` |
| Confirmed brief without a model | the frozen brief's `confirmed_at` and `story_id`, read back over the API, and a plan released only by `POST /product-briefs/{id}/admit` over tasks that were undispatchable before it | `mega-noop` |
| Nothing but the harness planned it | three durable observations — before the admission, after it and after engineering — that the brief's planning attempt is still this run's, that the claim was never finished out from under it, and that the story carries exactly the tasks this run planned | `mega-noop` |
| Confirmed settings reach the product | the deploy Run's per-setting `settings_seed`, the consumer's `deploy_settings_seed_brief` line with `route=story`, and the deployed product's own readback | `mega-noop` |
| Scripted product change | the story branch diff carries every change-set path, and the deployment answers the added endpoint, the registered product setting and the published bot command | `mega-noop` |
| Deployed artifact identity | the deploy Run's image references, tagged with `main`'s head as GitHub reports it, read before any QA attempt | all named suites |
| Execution evidence | `run_evidence` artifact and runner per-pair log/JUnit/TSV | all named suites; pair-specific for LLM/matrix |
| Failure attribution | the failing stage, its control-plane reason, the engineering Run records and the verdict | all named suites; the paid verdict rules apply to LLM/matrix |
| Diagnostics | bounded debug dumps, redacted service tails on suite failure, runner log, and public report files | all named suites |
| Ownership fence | `OwnershipManifest`, run labels, and fenced teardown | all named suites |
| Neighbour isolation | manifest-scoped cleanup regressions; no prefix or shared-stream deletion | all named suites |
| Redaction | stand acceptance admission scans only public evidence | all named suites |
| Cleanup verification | cleanup guard plus runner sweep; either failure is red | all named suites |

`tests/live/po_default_preflight.py` is retained as a separate operator preflight. It is not part
of `matrix` in this iteration, so no named suite currently claims PO-default coverage.

## LIVE_NO_CLEANUP

Set `LIVE_NO_CLEANUP=1` to leave a run's owned resources in place after teardown so a failed or
timed-out pipeline can be inspected live (target containers, GitHub repo, DB rows, registry, ports,
Redis entries). `cleanup_guard` then skips `cleanup_all` and logs a `cleanup skipped — resources
left for debugging` warning listing what remains. The run's primary error (assert or timeout) is
still raised unchanged — the flag only affects teardown, never the test result.

The ownership manifest is still written under `.live-manifests/<run_id>.json`, so `make
test-live-clean` can remove the leftovers once debugging is done. Without the flag, teardown stays
fail-closed exactly as above.

```bash
LIVE_NO_CLEANUP=1 make test-live-mega-noop   # leave resources for inspection on failure
make test-live-clean                         # remove them afterwards
```

The full pipeline has a separate post-deploy gate. Once the application is `running`, the harness
starts a health-only QA observation against `/health` and `/v1/health`. It accepts only the terminal
contract `status=completed` with `qa_outcome=passed`. An unreachable endpoint, a non-200 response or
timeout makes the live run red. This gate does not publish to `qa:queue` and does not run an LLM.

## Run evidence

Every mega run writes one machine-readable artifact for the worker/QA combination it exercised.
Local runs use `docs/e2e_results/run-evidence-<combination>-<timestamp>.json`; the stand runner writes
the same file into its run directory, transfers it before ephemeral-host cleanup, scans it for
protected values, and includes it in the final workflow artifact. It exists so a dynamic worker's
death remains attributable after the host is gone: it carries the deployed SHA and
the worker image digest record in use, the project, the role agents **as executed**, the attempt
count, the terminal state and failure kind, the duration, bounded/redacted task failure metadata —
and per worker container its exit code, a bounded log tail and the path of the transcript
worker-wrapper retained under `WORKER_TRANSCRIPT_STORAGE_PATH`.

**Workers are found by run label.** The collector (`tests/live/run_evidence.py`) is given one fact,
this run's id — the same `initiating_run_id` the project was created with — and asks

```
docker ps -a --filter label=com.codegen.type=worker --filter label=com.codegen.run.id=<run id>
```

Every worker the run causes carries that label from creation, so a pass that runs *after* a worker
died reads its exit code and log tail exactly as well as one that ran while it lived. No creation
window, no container-name prefix, no dependence on a poll landing in time.

What a label cannot survive is the *removal* of the container: `docker ps -a` forgets a removed
container, and worker-manager removes one on delete. No polling interval fixes that — a harness
cannot win a race against an asynchronous deleter — so the deleter captures instead. Before
`delete_worker` removes a container it reads its exit code, a bounded log tail, its image, its agent
type and its transcript directory into `worker:evidence:removed:<run id>`, a run-scoped Redis record
that the deletion of `worker:meta:<id>` does not touch. The collector reads it as its second source,
and it carries facts: a worker created and deleted before any pass ran still arrives with its exit
code, as `discovered_by: "delete_capture"`.

The run's ownership manifest is the third and weakest source, for a worker in neither of the other
two — no container and no record, because the capture itself never reached Redis. That case is why
`delete_worker` keeps `worker:meta:<id>` when it could not store the record: the manifest reads that
metadata, so the worker is still nameable when the container is gone. It contributes an
explicit `{"status": "missed", "reason": …}` record and nothing else. A worker is never omitted — an
omitted worker reads as "nothing ran", which is the failure this evidence exists to end. Evidence
collection never fails a run: a probe error, an unreadable removal record, or a failed ownership
refresh is recorded under `capture_errors`.

The QA cell reports three independent executor facts. `executor_requested` is what the runner asked
for. `executor_selected` is a capture read from the QA Run's persisted `executor_decision` — the
choice the API's resolver made at admission — so a run admitted under an executor nobody asked for
shows up as a disagreement; when no Run record could be read the field is a stated missed capture
naming why, never the request, because a field derived from the request agrees with it by
construction. `executor_executed` comes from the QA container this run's label selected, never from
the qa-worker's configured selector; that selector appears only in the missed capture's reason when
no QA container was seen. Run 33743251165 is why: it asked for `claude`, was admitted under `codex`
and ran on Codex, and the artifact reported `claude` twice.

The log tail is the container's own log (worker-wrapper's structlog), bounded and redacted through
`shared.diagnostics.redact_diagnostic` against the container's secret environment values. Agent
output never re-enters a result payload or a service log, and a **free** deterministic run keeps its
transcript a pointer: path and file list, no content.

A **paid** run retains three bodies per worker, whatever its outcome, because an artifact that
cannot say why a paid run went red is worth less than the residual disclosure risk of a bounded,
redacted body leaving a machine that is about to be destroyed — probe 2 of sprint 1429 ended with
"root cause not knowable from the artifact: worker transcripts live on the destroyed stand". They are
`transcript.content` (what worker-wrapper wrote under the transcript bind mount), `agent_report` (the
`REPORT.md` the control plane stored as a `worker_report` task event) and `branch_diff` (the change
the worker's branch carries, named by repository, branch and head SHA). Each is a capture: present,
or the stated reason it could not be collected — a QA executor writes no report and produces no
branch, and says so. `scripts/stand_acceptance.py` demands all three of any paid artifact.

There is no condition on the outcome, because the outcome is not knowable where the artifact has to
be written. A `stand-e2e` run is red for things this process never sees: `scripts/stand_run.py`
returns 1 when its mandatory sweep fails after every cell passed, and its hard-timeout path SIGKILLs
pytest. Four rounds of this card moved that gate around before it was deleted instead.

**What the artifact classifies.** `run_evidence.run_failure` answers whether this *combination*
succeeded — pytest's per-test reports (`suite_outcome`, fed by the conftest's
`pytest_runtest_logreport` hook), pytest's session exit status, and the pipeline's terminal state for
a phase that raised before any report existed. A completed pipeline whose assertion failed is red
with a `suite_failed` reason of its own, distinct from `run_failed` which names a stage, and
`failure.stage` stays `completed` because *where the pipeline stopped* is a different question. A
runner-level ending — a failed sweep, a hard timeout — is the workflow's verdict and is deliberately
**not** represented here; read the workflow for the run's result, and this file for the
combination's evidence.

The bodies are readable only while the stand exists, so they are collected, redacted and held before
teardown. The fixture's write is a crash-safety copy and `pytest_sessionfinish` rewrites it in place
once the in-process suite verdict exists.

Every byte is redacted **on the stand host** before the artifact crosses to the runner and **before**
any bound is applied to it: `_retained_body` is the single funnel for all three, and it redacts the
whole body line by line — the same helper and the same shape as the service-tail pipe — against every
value of the harness process environment whose name says it is a secret. Bounding first cannot be
made safe by widening a window, because `redact_diagnostic` replaces a whole known value and a value
straddling the cut would survive as a prefix. A body carrying a protected value that spans a line
break is withheld with a stated reason, since the line-by-line pass cannot see it whole; a redaction
that does not complete publishes its stated reason instead of its input.
`FAILURE_RETENTION_MAX_CHARS` bounds the redacted text, and a truncated body says so and names the
limit. `tests/live/test_run_evidence.py` covers the whole schema offline;
`tests/integration/backend/test_run_evidence_by_label.py` proves it against a real daemon, with a
worker killed and forgotten by Redis before anything reads it, and with one taken through the whole
ordinary delete path — container removed, metadata deleted — before anything observes it at all.

**The Product Brief block is what the run's own scenario owes.** `brief.obligations` names the
facts this run is judged on, and only those can make the verdict red. A paid confirmed-brief
variant owes the whole durable chain — confirmation, coverage, admission, the Architect criterion,
the settings readback and the deploy seed, and the job central QA fired; `mega-brief-package` owes
its deployment's `package_route` on top of it, because reading the active-package contract and the
generated job registry off the deployment is what entitles the run to claim the package path was
taken. The free level-1 lifecycle confirms a Product Brief through the released PO tools on every
run, so it owes that confirmation and is red without it; it publishes no Architect criterion and
its deterministic QA fires no product job, so it owes neither and the document says so instead of
the flat "this is not a Product Brief scenario" it used to answer to all seven fields.

The Architect criterion is judged against the expectation the *scenario declared*
(`BriefScenario.expected_criterion`) — the same terms its fixture refuses the run on — rather than
against one variant's job name. Stand run 34243255594 is why: `mega-brief-package` passed every
test, deployed, fired `reminders.tick` and was called red by an artifact that demanded
`multilingual_digest` (`issue:62bc9840e23a44c2098b`).

## Naming the failure

A run that stopped has to say **where** and **why** in the artifact itself, because by the time
anybody reads it the host is gone. Run 33683482667 is why: it ended `stopped_at_engineering` /
`worker_did_not_finish` with `attempts=0` and a null `failure_metadata`, and nothing that could
explain it survived teardown — no service logs, no engineering Run record, no admission outcome, no
executor diagnostics, and the test's own debug dump died with the deleted machine.

`failure` answers both questions. `stage` is the terminal state, `failure_kind` its classification,
and `control_plane_reason` is a capture: for the engineering stage it carries the Run's status, its
`error_message`, the `run_metadata` stop reason, the executor decision it was dispatched under and
the admission outcome that allowed it to exist, beside the task's redacted failure metadata and
iteration. When no Run could be read the field is a stated `missed` naming why — "the control plane
holds no engineering Run for it" is a different, and much sharper, finding than a blank field.

`engineering` carries the source those facts are read from: every engineering Run of the
combination, each with its Run record, its work-admission outcome and the executor diagnostics
snapshot in force at that moment. It is read inside the engineering phase, on failure exactly as on
success, because after teardown there is nothing left to read.

When that collection never ran at all, the section says which of the two things happened rather
than guessing: a run that entered no engineering phase created no Run for the artifact to read, and
a run that entered it and lost an error on the way out — a transient 5xx on an engineering poll,
say — may well have left Run records nobody read. The context already tells them apart, and a
canned line asserting the phase was never reached when the terminal state says `stopped_at_engineering`
is exactly the misstatement this artifact exists to remove.

`verdict` is `green` or `red` with the reasons for red, and it is where a paid run's *missing*
evidence stops being silent. On a paid combination, `worker_executed` coming back `missed` — and
`qa_executed` when the suite asked for an LLM QA executor — is a red reason carrying the
control-plane reason for the stage that stopped the run. The free `mega-noop` route starts no such
container by design, so its verdict is exactly what the terminal state always said.

The workflow fails closed on it. For a failed paid run, `scripts/stand_acceptance.py` refuses an
acceptance artifact whose run evidence lacks the failing stage, its reason, the engineering section,
any worker's three retained bodies, or the redacted service log tails — the same admission the handoff already fails closed on, and the
same redaction canary still guards it. A piece that genuinely could not be collected is admissible
*as a stated missed capture*; a piece that is simply absent is not.

`qa` and `deployment` are where a run that got as far as QA says what stopped it. `qa.run_record`
is the terminal QA Run itself, so a `blocked` outcome arrives with the blocker the consumer wrote —
its category, what QA attempted, what it sent and what came back — instead of the word `blocked`
alone. `deployment.run_record` is the deploy Run beside it, carrying the `smoke_result` that made
that deploy a success. `deployment.reachability` holds the three reads of the deployed URL this run
has: the deploy's own smoke, the harness's HTTP probe from the orchestrator host, and QA's probe,
whose `reached_the_url` separates "QA received nothing" from "QA read a response and rejected its
content" — and says which of the two readings it is, because only the first is observed (QA's own
blocker category) while the second is inferred from QA reaching a product verdict. Each is a
capture: an unread probe is a stated missed one, never a blank.

The QA Run is read wherever the run ends. Normally that is inside the QA wait; a run that left the
phase earlier — the harness health probe raises when the deployed URL does not answer, which is
exactly the shape this section exists for — has it read again before teardown. Nothing asserts what
was not looked at: "no QA run reached a terminal state" is published only when something actually
listed this story's runs, and `qa.run_record_source` says where the record came from.

What none of those reads can see is the application container itself, which lives on the *target*
machine. So when a deploy reported success and one of them got nothing, the artifact says so in
`deployment.reachability.target_host_snapshot` and the suite takes `target-app.log` — a bounded,
redacted `docker ps -a`, `docker inspect` state and log tail — from the target host **inside the
phase, before `cleanup_all` runs**. That deadline is not the machine's deletion: teardown's first
step streams the remote cleanup script to the target, which removes those containers, and
`docker ps -a` cannot list a container that was removed rather than stopped. The requirement flag is
the single predicate: the suite collects on it, `stand-e2e.yml` carries the file out of the runner
directory with the run evidence and names it when one was asked for and did not arrive, and
`scripts/stand_acceptance.py` refuses a paid failure that asked for the snapshot and can say neither
what became of it nor where it is. The free `mega-noop` route asks for nothing from the target host.

Two collectors feed it. `stand-e2e.yml` pulls redacted `docker compose logs` tails of all three
scheduler services, `engineering-worker`, `worker-manager`, `worker-broker`, `api`, `qa-worker` and `deploy-worker` into
`suite-services.log` when the suite fails — through the same `shared.diagnostics.redact_diagnostic` helper and the same
protected-name allow-list the provisioning-failure branch uses, with the stated reason published in
place of the tails if that pipe cannot complete. And `dump_debug` now writes beside the run evidence,
in the runner-owned directory the workflow collects from, rather than under the ephemeral checkout;
the artifact lists the dumps the run wrote under `debug_dumps`. A dump that does not reach the
handoff is named there too: acceptance refuses a candidate whose `debug_dumps` names a file it does
not carry, and a dump-shaped file whose name the allow-list cannot admit is reported as
`debug_dump_name_unadmissible:<name>` instead of being dropped in silence.

Because the dump now crosses that boundary, it is held to the boundary's rule. It embeds container
stdout, and a worker's stdout is not a trusted surface: worker-manager gives the worker an origin of
`https://x-access-token:<token>@github.com/...`, so an ordinary git failure prints a usable
credential into the log the dump copies. The embedded slices are bounded by the same
`run_evidence` bounds the worker log tails use, and the assembled text goes through
`shared.diagnostics.redact_diagnostic` against every secret-named value of the environment before it
is written. That is the same mechanism `redacted_payload` and the service tails already use, one
place further along — not a new secret-handling path. It redacts at write time rather than relying
on the admission canary, which scans only for a PEM marker and for the named stand values, and whose
match refuses the whole artifact rather than repairing one file.

When no service tails reach the runner at all, `suite-services.log` says what the workflow observed
— which collection step failed, and whether the suite step ran — and never a cause it did not
observe: an unreachable host, an `api` container that could not execute the redaction and a suite
that never ran after a provisioning failure are three different reasons for the same absent file.

## Run-scoped cleanup

Teardown removes what this run's ownership label selects, not what its context still remembers.
`tests/live/run_cleanup.py` is given one fact — the run id — and asks

```
docker ps -a       --filter label=com.codegen.run.id=<run id>
docker network ls  --filter label=com.codegen.run.id=<run id>
```

That covers a run's worker containers, its QA-egress proxies and its `dev_proj_<worker_id>`
networks, all of which worker-manager stamps with the run at creation. So a container nothing
recorded, a network whose worker id is no longer knowable, and everything left by a harness that
died mid-run are all found and removed. The label is the fence as well as the finder: a listed
resource whose `com.codegen.run.id` is not this run is refused rather than removed, and the
long-lived service containers carry no run label at all, so a cleanup scoped to one run cannot
touch a neighbouring run or the stack. Running it twice is not an error — every removal treats
"already absent" as success — and it verifies afterwards by asking the same two queries again,
raising `RunCleanupError` if anything is still selected.

`worker:meta:<id>` is the exception, and deliberately so. `delete_worker` retains that key when a
worker's removal record could not be stored, because it is then the last thing that can name the
worker to its run. Cleanup deletes such a key only for a worker this run's evidence already has a
record for (`RunEvidenceCollector.accounted_workers`), and otherwise keeps it and says so in its
report — expected residue, never swept as an anomaly. A run with no evidence of its own takes one
capture pass first and retains it under `.live-manifests/evidence/<run id>.json`; capture always
precedes removal. The run's removal records (`worker:evidence:removed:<run id>`) are evidence and
are never deleted by cleanup — they expire on their own TTL.

`scripts/clean_live_tests.py` starts its recovery of every manifest with exactly this sweep, so a
run's Docker resources no longer depend on the reconstructed `ctx` round-trip that follows it.

Two properties make "capture before cleanup" hold under more than one pass, because recovery is
more than one pass — the label sweep above, and the `ctx` round-trip after it.

*The artifact only ever gains.* `retain_evidence` merges into `.live-manifests/evidence/<run
id>.json` rather than replacing it: a record is added, or replaced by one that knows more, and
never by one that knows less. The second pass runs when the containers, removal records and
metadata the first pass read are already gone, so it knows almost nothing; without the merge it
would erase the very accounting that authorised their removal.

*Nothing is removed before the artifact names its worker.* `clean_run` checks every listed
container and network against `accounted_workers` and keeps — loudly — anything whose worker has no
record, its Redis keys included. A capture that fails is not a licence to remove: a caller uses
`account_listed_workers` first, which writes the failure down as a missed capture naming the worker
and why its ending could not be read. That is an acceptable ending; a worker that simply disappears
is not.

## Database teardown, derived from the catalog

The rows a run leaves behind are removed by `tests/live/db_teardown.py`, which is given the run's
roots — its project id, and the Telegram id of the user it registered when it has one — and reads
the rest out of `pg_constraint`. Starting at those rows it walks *incoming* foreign keys, so what
the run owns is what the keys say points at it, and it follows only
the edges the database would refuse (`NO ACTION`, `RESTRICT`); a child the schema removes or unlinks
by itself (`CASCADE`, `SET NULL`) is neither deleted here nor expected to be gone, which is why
`engineering_attempt_ledger` is outside a project-rooted closure (it FKs `runs`, `projects`,
`stories` and `tasks` with `SET NULL`) and enters a user-rooted one, where it is retained rather
than deleted — see the retention rule below; a table reachable only
through a `CASCADE` edge is out of the plan for the same reason, and when one appears the database
refuses the delete and the error names the constraint, so the gap is loud rather than silent. The
deletion order is the
reverse topological order of that closure, in one transaction, and a cycle between two tables is
raised by name rather than guessed at.

**The plan is built from foreign keys and only foreign keys — and the schema has columns that are
not one.** `service_deployments.project_id` is denormalized from the application and deliberately
carries no key (`shared/models/deployment.py`), and its `application_id` is nullable, so a row can
name a run's project and be reachable through no key at all: nothing deletes it, nothing refuses,
and a proof built from keys alone would call the teardown clean. Such columns are therefore derived
too, never listed. The schema's own foreign keys say what a column name means — `project_id` is the
name a dozen tables use for `projects.id` — so any *other* column of that name carrying no key of
its own is treated as the reference it is, and becomes a predicate and an ordering edge like any
foreign key. That covers `service_deployments` and `api_keys` today, and covers the next
denormalized column by existing. Two rules keep it honest: a name that resolves to more than one
parent inside the closure is raised rather than guessed at, and an explicit key outranks an inferred
one — a table the schema unlinks with its own `ON DELETE SET NULL` key into the closure, such as the
deliberately-retained `engineering_budget_reservations`, is left alone however its other columns are
named.

**The run's user is a row the level-1 run owns, and it is a second root.** The level-1 run registers
itself through the product's door — `register_run_owner` mints a promo code and redeems it at a
fresh Telegram id from the band the harness registers in
(`live_harness.RUN_USER_TELEGRAM_ID_MIN..MAX`) — so the rows that hang off that user without hanging off its project are the run's residue:
`engineering_budget_policies`, `engineering_budget_reservations`, the `promo_codes` row it redeemed,
`work_admission_audits`, its `rag_*` dialogue rows. They join the closure because the *root* does,
not because anyone listed them; `work_admission_audits.user_id` carries no foreign key at all and is
covered by the same denormalized-column derivation as `service_deployments.project_id`.

A root's predicate is the caller's subject and nothing widens it. `projects.owner_id` points at
`users`, so the user root reaches the project root — that edge orders the two (a project goes before
its owner) and selects nothing, which is why a run deletes the project it named rather than every
project its owner happens to have.

The other suites — scaffold, engineering, brief, LLM — still share one fixture user at a fixed
`TEST_TELEGRAM_ID`. Their teardown passes no user predicate, so there is no user root, and their
regime is unchanged: the fixture is nobody's to delete and nothing hanging off it joins the closure.

**Two of the run's own rows cannot be deleted, and the plan says so rather than trying.**
`engineering_attempt_ledger` is append-only by the trigger
`engineering_attempt_ledger_append_only`, and its `user_id` foreign key is `NO ACTION`, so the run's
`users` row cannot go either while its attempts exist. Both are declared in
`db_teardown.RETENTION_RULES`: no `DELETE` is issued for them, they are inventoried before the
deletes and read back by predicate afterwards, and teardown reports them by table, key and count.
The retained set has to be *exactly* the declared one — for a run's own teardown, exactly one `users`
row for its Telegram id plus the ledger rows of its own engineering attempts — and anything else
fails the teardown. It is a stated rule, not a swallowed error: no trigger is disabled and no schema
or product contract changes to accommodate it (whether it should is issue:792460b9934c749050ce).

The stand sweep carries the same second root, selected by what the harness wrote rather than by one
id. That is the backstop for a run that died between registering and creating its project: such a
run owns a user, a code and a policy and no project at all, so nothing the title prefixes select
could ever find it.

Two things bound that root, and the distinction between them matters. The Telegram id band
(`live_harness.RUN_USER_TELEGRAM_ID_MIN..MAX`) is where runs register, but it is **not** ownership:
Telegram issues account ids and a real customer can hold one anywhere inside it, so selecting on the
band alone would be a blind range delete over strangers' budgets, codes, audits and dialogue rows.
What the harness genuinely owns is the *username* it registers under — `live_run_<telegram id>`,
written by `register_run_owner` and by nothing else — the same kind of naming as a contour's project
title prefix. `run_user_sweep_predicate()` requires both, so a row inside the band that the harness
did not name is left where it is.

On top of that, the sweep takes the user root **only in a contour that owns live runs**. `make
test-live-clean` runs the sweep with `LIVE_CONTOUR` unset, which is the prod contour — the one whose
refusal says it "holds real users' data". Nothing registers run-owned users there, so the production
sweep keeps exactly the regime it had before the registration door existed: projects by title
prefix, plus the single fixture-user statement, and no user root at all.

Two bounds this backstop does not cover, stated rather than implied. A registration that mints a
code and is then refused (the criterion-5 path) raises before the run has a user, so its unredeemed
`promo_codes` row is reachable by neither teardown path — `redeemed_by_user_id` is NULL — and stays
as one unusable row per refused registration; the run fails loudly, so an operator sees it. And a
run that dies after registering but before its project exists has no per-run teardown at all
(`cleanup_guard` is installed after `create_project`), so its user's rows depend entirely on this
sweep.

The proof is the same plan read back. Every key the run owns is recorded *before* the deletes and
asked for again afterwards, so the check still answers once the project row is gone; anything that
answers is raised as its table, its key and the constraint by which it belongs to the run. A delete
the database refuses is reported as the constraint, the table it is on and whether the plan knew
about that table at all — a table outside the plan means the catalog it was built from is stale.

This replaced a hand-written list of `DELETE` statements, which went stale silently and in the
direction of leaving residue behind: run 35441716423 could not delete its project because the
level-1 grant deploy had written a `users_grant_intents` row referencing its `deploy-grant-…` run,
and that table was in nobody's list. `tests/live/test_db_teardown.py` holds both properties offline,
against this schema's own metadata (`shared/tests/project_cleanup.py::metadata_catalog_payload`).

## The run proves it left nothing, and needed nobody

Two proofs the level-1 lifecycle takes about itself, both built on `run_proof.py` — one question,
one source, three answers: **absent** (asked and found nothing), **leftover** (asked and found
things), **unaskable** (could not ask). The third is the point. An unreachable target, an unreadable
registry or a Redis that refused the query fails the run naming the kind it could not check, never
passes it quietly; this is the distinction card 1318 had to add when an unreadable manager log was
rendering as an empty log. Every probe therefore raises on a non-answer — a non-zero exit, a missing
marker, an unparseable payload — instead of returning an empty finding list, and a kind that no
check answered at all is reported as *unasked* and fails too, so the proof cannot shrink by losing a
probe.

**And a question no reachable state could answer yes to is not a passing question either.** That is
the same defect one level up, and it is how the first version of this proof failed review: the PO
checkpoint kind asked `thread_id = <run id>` when nothing in the repository ever checkpoints under a
run id, so it reported `absent` on every possible run. Two things answer it now. The PO kind asks
the thread the consumer really writes (below), and refuses to answer without the snapshot that makes
the run's own rows knowable. And `run_residue.vacuity_notes` names, in a green proof's own notes,
every kind whose subject list was empty — a run that owns no deployed stack passes
`target_containers` whatever is on the target, and a reader is told so instead of trusting the label.

**Nothing left** (`run_residue.py`, asked by `cleanup_and_prove` after `cleanup_all` succeeds). One
question per kind the Definition of Done names: containers on the control host *and* on the target,
image repositories in the registry, workspaces, Redis keys, the GitHub repository, the PO checkpoint
thread, and the database rows. This asks about *kinds*, not about removals, which is what makes it
different from the verification each removal already does — a kind nothing removes is invisible to
those. Two such kinds exist today:

- The one-shot containers `docker compose run` creates inside a worker's bounded compose plan.
  `docker compose down -v` removes the plan's services and not these, and exited
  `*-integration-tests-run-*` containers survived a completed story on production for 7+ hours and
  then survived a whole project teardown (`issue:868e40fc0377b0dabb77`). They carry no
  `com.codegen.run.id`, so the run-label query cannot see them; they are asked for by the label
  Compose does stamp, the worker's own project name. Worker-manager now removes them too, in the
  worker's teardown and in the orphan collector (`shared/worker_compose.py`).

  **A level-1 run creates none of these**, and the assertion is made anyway because the Definition of
  Done names them. The level-1 developer path is the scripted `NoopRunner`, whose only product step
  is `make setup` (`packages/worker-wrapper/src/worker_wrapper/runners/noop.py`), and `make setup` in
  the pinned kit runs `uv sync`, `framework.generate` and `ruff` — no `docker compose` at all.
  `make test-integration` is run only by a real developer agent, which
  `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` tells to; that is the path the
  issue's production evidence comes from, and the fix is for it.
- The project workspace. A developer worker's checkout is deliberately preserved across its own
  teardown so the next attempt reuses it, so nothing ever took it away when the project went;
  `shared/live_harness_workspaces.py` removes the run's entries inside worker-manager and reads the
  filesystem back.

- The PO conversation rows. The thread is `po_thread_id(telegram_chat_id)` → `po-chat-<chat id>`
  (`shared/contracts/queues/po.py`), the only value the PO consumer passes as a checkpoint thread id,
  and it is composed from the chat id the PO tools are invoked with — so `capture_run_po_position`
  is given the actor, never assuming one. For a level-1 run that actor is the user the run
  registered for itself, so the thread is the run's own and nothing else has ever written to it; for
  a run that shares the fixture user the thread is a **fixture every live run shares**, not the
  run's to delete, while the rows that appear on it during the run still are. One mechanism serves
  both: `po_checkpoints.py` takes a snapshot of the thread before the project exists, removes the
  difference after cleanup, and asks the same predicate again; the thread is left exactly as the run
  found it, head checkpoint and channel versions intact. Without that snapshot the kind is reported
  as one that *could not be asked* — never as absent. The bound the shared case leaves is stated
  there: "appeared during this run" is the run's rows only while no other run writes to the same
  thread at the same time, which the stand's one-suite-at-a-time schedule holds. Level 1's own
  registered identity removes that question for level 1 entirely; the suites that still share the
  fixture keep it.

The database kind is **not re-asked**: `cleanup_all` hands the residue proof the `TeardownReport`
that the catalog-derived teardown above already produced, so the one place that knows how to ask the
database stays the only place that asks it, and the check carries the tables and key count it
proved — and, when the plan retained rows, those by table, key and count too, so the one `users` row
and the ledger rows a level-1 run leaves by declared rule are named in the proof rather than hidden
behind the word `absent`. That kind goes red in `cleanup_all` — before this proof is reached — which
is the card's instruction rather than an accident, and is said so in `database_check_from`. The one Redis key a
clean run keeps is `worker:evidence:removed:<run id>` — the removal records are evidence and expire
on their own TTL — and it is excluded by name, with the reason in the proof's notes.

**Nobody needed** (`run_intervention.py`, recorded before teardown). No story of the run ever entered
`waiting_human_review`, `waiting_user_secret` or a quarantine. *Ever*: a story that parked and was
then recovered ends `completed` and has its `quarantine_reason` cleared, so the terminal state
cannot answer this. What survives the recovery is the owner notification the park published onto
`po:input`, read from a cursor the run captures before its project exists — which is why this runs
ahead of teardown, since teardown XDELs the run's own stream entries. Which events count is not a
list somebody maintained: every park owes its owner a notice through
`owe_owner_notification(..., terminal_status=StoryStatus.WAITING_*)`, and
`test_run_intervention.py` reads `services/` for that call shape and fails when a producer appears
that `INTERVENTION_EVENTS` does not know. That scan is what added `story_impossible_capacity` and
`task_impossible_capacity`, both of which park a story in `waiting_human_review`.

The stories' current state is the second source, for a park whose notification never reached the
stream: status, `quarantine_reason`, and the still-owed `owner_notification`. That last one is read
through `GET /api/stories/{id}/owner-notification` and **not** out of the story listing, because
`StoryRead` does not declare the field and FastAPI drops it — reading it from the listing was a
third check that asserted nothing, and `test_run_intervention.py` now drives the real helper against
a fake transport so the route it asks is part of the contract.

Offline coverage for both, kind by kind, is in `tests/live/test_run_residue.py`,
`tests/live/test_po_checkpoints.py` and `tests/live/test_run_intervention.py`; the pieces outside
`tests/live` are in `shared/tests/test_run_residue_probes.py` and
`services/worker-manager/tests/unit/test_compose_residue.py`.

## Bot access revocation

`tests/live/test_bot_access_revocation.py` is the only check that asks the deployed bot whether a
revoked identity is really refused; everything else reads the values a deploy would ship. It needs
a project already deployed with a private bot whose commit declares the test identity slot:

```bash
BOT_ACCESS_PROJECT_ID=<project-uuid> uv run pytest tests/live/test_bot_access_revocation.py
```

It records a grant, lets the scheduler sweep deploy it, sends `/start` from the QA account with
the same probe the QA runner uses, then cancels the QA run mid-flight and requires the bot to
refuse that account once the sweep reports the grant revoked. The test never clears the value
itself — a cleanup it performed would prove its own cleanup, not the pipeline's. Two real deploys,
so it is excluded from the offline live regressions.