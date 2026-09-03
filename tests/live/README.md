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
body already failed.

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
| `mega-noop` | `tests/live/test_full_pipeline.py::TestFullPipeline` | 0; two noop engineering Tasks and deterministic QA | 1 | one project; paid admission evidence; two ordered noop Tasks on one Story worker; deploy; deterministic QA; completed Story/PO record; explicit undeploy | manifest-owned, fail-closed, then product undeploy verifies port release | 75 min | measured from stand artifacts; no baseline measurement yet |
| `mega-llm` | `tests/live/test_full_pipeline.py::TestFullPipelineLLM` | one developer + one QA executor turn | 1 selected `--worker` / `--qa` pair | one project; selected developer; deploy; selected QA executor | manifest-owned, fail-closed | 60 min | measured from stand artifacts; no baseline measurement yet |
| `mega-brief` | `tests/live/test_product_brief_pipeline.py::TestProductBriefPipeline` | one Architect, developer and QA executor turn | 1 selected `--worker` / `--qa` pair | confirmed Product Brief; Architect coverage/admission; selected developer; deploy settings seed; selected QA executor | manifest-owned, fail-closed | 90 min | measured from stand artifacts; no baseline measurement yet |
| `matrix` | `tests/live/test_full_pipeline.py::TestFullPipelineLLM` | 8 total: developer + QA for each cell | 4: Claude/Codex QA × Claude/Codex developer | one complete LLM pipeline per cell | after every pytest cell and a final runner sweep, both fail-closed | 60 min per cell | measured from stand artifacts; no baseline measurement yet |

The local target names reflect that same contract:

- `make test-live-mega-noop` runs only the noop class.
- `make test-live-mega` is a compatibility alias for `test-live-mega-noop`.
- `make test-live-mega-llm` runs only the LLM class for one locally configured pair.
- `make test-live-mega-brief` runs only the Product Brief E2E class for one locally configured pair.
- `make test-live-matrix` delegates the four paid cells to the stand runner.
- `make test-live-pipeline` is a legacy aggregate of scaffold, engineering, and both full-pipeline
  classes. It is not a named suite and intentionally remains visible until duplicate coverage is
  removed in a later iteration.

### Switching the QA executor

An LLM cell asks for a QA executor, and the runner has to make that true before pytest starts.
`QA_EXECUTOR_AGENT_TYPE` is written to the deployed `.env`, and every compose service the variable
is passed to is force-recreated — the set is read out of the compose files by
`stand_run.qa_executor_services`, never transcribed, so a service that starts reading it tomorrow is
recreated without anyone editing the runner. Today that set is `api` and `qa-worker`.

The recreate does not return until every service it recreated is usable the way the suite will use
it: `stand_run.recreate_and_wait` is the runner's only way to bring a container up — `_compose`
refuses `up`, `start` and `restart` anywhere else — so recreating and waiting cannot be separated by
a later caller. `api` is asked for `GET /health` on `http://localhost:8000`, the base URL
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

The timeout values are deliberate bounds, not duration estimates. The noop lifecycle's explicit
waits sum to at most 61m20s (`120 + 840 + 60 + 420 + 420 + 120 + 320 + 300 + 180 + 180 + 120 + 300 +
300` seconds): scaffold; two ordered noop Tasks; Story aggregation; deploy; a bounded public health
probe (up to two 30-second paths per attempt); deterministic QA; completed-story and durable PO
delivery; the exact deployment record; then undeploy Run, terminal application, and port-allocation
release. The 75-minute cap leaves 13m40s for manifest-owned teardown and diagnostics. The LLM pipeline
remains 53 minutes (`120 + 1800 + 420 + 420 + 120 + 300`) because it does not yet run the new lifecycle
acceptance. `mega-brief` has a 90-minute cap because it adds an Architect planning turn and may
release multiple sequential engineering Tasks before the deploy and QA stages. A recreate's readiness wait and the QA executor switch that follows it are separately
limited to three minutes each; runner preflight and final sweep are each five minutes.

For the largest workflow path, provisioning has a 45-minute budget. Its configured waits include
two 10-minute machine allocations, five minutes for DNS, three minutes for API readiness, and 20
minutes for target provisioning; the remainder is bootstrap/Ansible reserve. The previous broad
control-plane bootstrap measured about seven minutes. It now uses a stand-only minimal playbook
whose expected 2–3 minute duration is pending live confirmation; that expectation does not change
the overall provisioning budget. The matrix runner is bounded at 274 minutes (`5m preflight + 4 ×
(60m cell + 3m readiness + 3m switch) + 5m sweep`). The E2E job cap is 360 minutes, a strict
41-minute reserve over provisioning plus that runner path. Lifecycle cleanup runs in its own 30-minute GitHub job, because
jobs do not share an outer timeout.

### Invariant map and first-iteration baseline

All named suites exercise the product acceptance path: project creation, scaffold, engineering,
deploy, and QA verdict. `mega-noop` additionally proves each Task's admitted paid-run audit,
immutable noop `ExecutorDecision`, typed terminal result, canonical zero-provider-cost ledger row,
and actual reservation outcome; its second `todo` Task is blocked by the first and must not receive
a Run early. The two Tasks complete through one observed Story-worker lifecycle before the PR/merge
can lead to deploy. It also proves the completed Story's durable `story_completed` owner record, its
matching post-cursor PO input event and verified public URL, the successful service deployment's exact
merged SHA, and a product API undeploy through terminal `not_deployed` plus owned port-allocation
absence. Every named suite also compares the deploy Run's image references with the commit `main` points at —
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
LIVE_NO_CLEANUP=1 make test-live-mega   # leave resources for inspection on failure
make test-live-clean                    # remove them afterwards
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

Agent stdout stays out of it. The log tail is the container's own log (worker-wrapper's structlog),
bounded and redacted through `shared.diagnostics.redact_diagnostic` against the container's secret
environment values; Codex CLI diagnostics stay in the retained transcript, which the artifact
references by path only. `tests/live/test_run_evidence.py` covers the whole schema offline;
`tests/integration/backend/test_run_evidence_by_label.py` proves it against a real daemon, with a
worker killed and forgotten by Redis before anything reads it, and with one taken through the whole
ordinary delete path — container removed, metadata deleted — before anything observes it at all.

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
acceptance artifact whose run evidence lacks the failing stage, its reason, the engineering section
or the redacted service log tails — the same admission the handoff already fails closed on, and the
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

Two collectors feed it. `stand-e2e.yml` pulls redacted `docker compose logs` tails of `scheduler`,
`engineering-worker`, `worker-manager`, `worker-broker`, `api`, `qa-worker` and `deploy-worker` into
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
