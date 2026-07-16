# Live harness contract

Pipeline tests create one `OwnershipManifest` per run. The manifest is written under
`.live-manifests/` and records exact project, GitHub repository, Redis entry, port allocation and
server deployment identifiers as they become known. Teardown addresses only those identifiers.
It never deletes a shared Redis stream, scans all configured servers or matches resources by the
historical `live-test-*` prefix.

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
