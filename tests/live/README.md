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
| `mega-noop` | `tests/live/test_full_pipeline.py::TestFullPipeline` | 0; noop engineering and deterministic QA | 1 | one project; noop engineering; deploy; deterministic QA | manifest-owned, fail-closed | 40 min | measured from stand artifacts; no baseline measurement yet |
| `mega-llm` | `tests/live/test_full_pipeline.py::TestFullPipelineLLM` | one developer + one QA executor turn | 1 selected `--worker` / `--qa` pair | one project; selected developer; deploy; selected QA executor | manifest-owned, fail-closed | 60 min | measured from stand artifacts; no baseline measurement yet |
| `matrix` | `tests/live/test_full_pipeline.py::TestFullPipelineLLM` | 8 total: developer + QA for each cell | 4: Claude/Codex QA × Claude/Codex developer | one complete LLM pipeline per cell | after every pytest cell and a final runner sweep, both fail-closed | 60 min per cell | measured from stand artifacts; no baseline measurement yet |

The local target names reflect that same contract:

- `make test-live-mega-noop` runs only the noop class.
- `make test-live-mega` is a compatibility alias for `test-live-mega-noop`.
- `make test-live-mega-llm` runs only the LLM class for one locally configured pair.
- `make test-live-matrix` delegates the four paid cells to the stand runner.
- `make test-live-pipeline` is a legacy aggregate of scaffold, engineering, and both full-pipeline
  classes. It is not a named suite and intentionally remains visible until duplicate coverage is
  removed in a later iteration.

### Timeout budget

The timeout values are deliberate bounds, not duration estimates. The pipeline's explicit waits
sum to 30 minutes for noop (`120 + 420 + 420 + 420 + 120 + 300` seconds) and 53 minutes for LLM
(`120 + 1800 + 420 + 420 + 120 + 300`). The 40- and 60-minute pytest caps add time for
manifest-owned teardown and diagnostics. A QA executor switch is separately limited to three
minutes; runner preflight and final sweep are each five minutes.

For the largest workflow path, provisioning has a 45-minute budget. Its configured waits include
two 10-minute machine allocations, five minutes for DNS, three minutes for API readiness, and 20
minutes for target provisioning; the remainder is bootstrap/Ansible reserve. The matrix runner is
bounded at 262 minutes (`5m preflight + 4 × (60m cell + 3m switch) + 5m sweep`). The E2E job cap is
360 minutes, a strict 53-minute reserve over provisioning plus that runner path. Lifecycle cleanup
runs in its own 30-minute GitHub job, because jobs do not share an outer timeout.

### Invariant map and first-iteration baseline

All named suites exercise the product acceptance path: project creation, scaffold, engineering,
deploy, and QA verdict. The LLM suites additionally prove selected executor wiring; the noop suite
is intentionally the free deterministic counterpart. The static baseline at this point is one
noop run, one selected LLM pair, or four unique matrix pairs; it does not claim unmeasured wall
times.

| Invariant level | Primary evidence | Suites |
| --- | --- | --- |
| Product acceptance | `TestFullPipeline` / `TestFullPipelineLLM` status, deploy, health, and QA assertions | all named suites |
| Execution evidence | `run_evidence` artifact and runner per-pair log/JUnit/TSV | all named suites; pair-specific for LLM/matrix |
| Diagnostics | bounded debug dumps, runner log, and public report files | all named suites |
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

Every mega run writes one machine-readable artifact for the worker/QA combination it exercised, to
`docs/e2e_results/run-evidence-<combination>-<timestamp>.json` (git-ignored, retained on the host).
It exists so a dynamic worker's death is attributable after the run: it carries the deployed SHA and
the worker image digest record in use, the project, the role agents **as executed**, the attempt
count, the terminal state and failure kind, the duration — and per worker container its exit code, a
bounded log tail and the path of the transcript worker-wrapper retained under
`WORKER_TRANSCRIPT_STORAGE_PATH`.

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

The QA cell reports `executor_executed` from the QA container this run's label selected, never from
the qa-worker's configured selector: that selector is reported separately as `executor_selected`,
and appears in the missed capture's reason when no QA container was seen.

Agent stdout stays out of it. The log tail is the container's own log (worker-wrapper's structlog),
bounded and redacted through `shared.diagnostics.redact_diagnostic` against the container's secret
environment values; Codex CLI diagnostics stay in the retained transcript, which the artifact
references by path only. `tests/live/test_run_evidence.py` covers the whole schema offline;
`tests/integration/backend/test_run_evidence_by_label.py` proves it against a real daemon, with a
worker killed and forgotten by Redis before anything reads it, and with one taken through the whole
ordinary delete path — container removed, metadata deleted — before anything observes it at all.

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
