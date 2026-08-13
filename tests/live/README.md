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
container, and worker-manager removes one on delete (before deleting its Redis metadata, so nothing
here may lean on Redis either). Two things follow. `pipeline_helpers.evidence_pass` takes a pass on
every engineering poll and every poll of the post-deploy QA wait, so a pass always lands before the
removal; and the run's ownership manifest is reconciled in as a second source, contributing a worker
the label query never listed as an explicit `{"status": "missed", "reason": …}` record. A worker is
never omitted — an omitted worker reads as "nothing ran", which is the failure this evidence exists
to end. Evidence collection never fails a run: a probe error, or a failed ownership refresh, is
recorded under `capture_errors`.

The QA cell reports `executor_executed` from the QA container this run's label selected, never from
the qa-worker's configured selector: that selector is reported separately as `executor_selected`,
and appears in the missed capture's reason when no QA container was seen.

Agent stdout stays out of it. The log tail is the container's own log (worker-wrapper's structlog),
bounded and redacted through `shared.diagnostics.redact_diagnostic` against the container's secret
environment values; Codex CLI diagnostics stay in the retained transcript, which the artifact
references by path only. `tests/live/test_run_evidence.py` covers the whole schema offline;
`tests/integration/backend/test_run_evidence_by_label.py` proves the discovery against a real daemon
with a worker that is killed and forgotten by Redis before anything reads it.

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
