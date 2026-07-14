# Live harness contract

Pipeline tests create one `OwnershipManifest` per run. The manifest is written under
`.live-manifests/` and records exact project, GitHub repository, Redis entry, port allocation and
server deployment identifiers as they become known. Teardown addresses only those identifiers.
It never deletes a shared Redis stream, scans all configured servers or matches resources by the
historical `live-test-*` prefix.

Cleanup is part of the test result. Every delete command must succeed and each owned resource must
then be observed as absent. A delete or verification error fails the run, including when the test
body already failed.

The repository root is derived from `tests/live/live_harness.py`. `ORCHESTRATOR_ROOT` may override
it, but the target must contain `pyproject.toml` and `tests/live`.

The full pipeline has a separate post-deploy gate. Once the application is `running`, the harness
starts a health-only QA observation against `/health` and `/v1/health`. It accepts only the terminal
contract `status=completed` with `qa_outcome=passed`. An unreachable endpoint, a non-200 response or
timeout makes the live run red. This gate does not publish to `qa:queue` and does not run an LLM.
