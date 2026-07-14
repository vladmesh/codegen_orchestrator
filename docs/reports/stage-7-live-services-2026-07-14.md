# Stage 7 live services validation, 2026-07-14

## Result

Stage 7 is RED. The run stopped at the mandatory preflight without creating external resources.
The live environment and credentials required to prove provisioning, worker lifecycle, deploy,
readiness, QA and cleanup were unavailable. Running the existing pipeline harness would also be
unsafe because its command root does not exist in this workspace and cleanup failures are not
terminal.

Stage 7 remains open. Stage 8 Telegram E2E is not unblocked.

## Reproducibility record

- Preflight timestamp: `2026-07-14T10:16:34Z`
- `codegen_orchestrator` commit: `efed54975d76f0bc883800983e5f869253e031f2`
- Production template source: `gh:vladmesh/service-template`
- Production template ref: `0.3.0`
- Resolved template tag object: `193afd054f24781598be50f5ff8d652d522e9482`
- Resolved template commit: `27c76cef41084fc20400342010714234e76b8f7f`
- Intended live entrypoints: `make test-live-smoke`, `make test-live-engineering`,
  `make test-live-mega`, followed by ownership-scoped verification equivalent to
  `make test-live-clean`
- No neighboring `service-template` checkout was present or used.

The tag was resolved with:

```text
gh api repos/vladmesh/service-template/git/ref/tags/0.3.0 \
  --jq '.object.type + " " + .object.sha'
tag 193afd054f24781598be50f5ff8d652d522e9482
gh api repos/vladmesh/service-template/git/tags/193afd054f24781598be50f5ff8d652d522e9482 \
  --jq '.object.type + " " + .object.sha'
commit 27c76cef41084fc20400342010714234e76b8f7f
```

The expected test scope would create unique `live-test-<random>` project and repository names,
one project UUID, stories/tasks/runs, a worker container and network, a workspace, a temporary
GitHub repository in `project-factory-organization`, a port allocation, and a Compose deployment
on a selected test server. No such resource was created in this attempt.

## Stage outcomes

| Stage | Entrypoint | Expected | Actual |
|-------|------------|----------|--------|
| Environment preflight | `docker compose ps --format json`; presence-only credential check | Running live stack and required secrets | RED at `2026-07-14T10:16:35Z`: no services were listed, `.env` was absent, and required GitHub, provisioning/deploy and encryption credentials were absent. No secret values were read or logged. |
| Provisioning | Live provisioner queue and terminal result consumer | Typed success or concrete terminal failure, never unbounded pending | NOT RUN. Mandatory infrastructure/credentials were unavailable. |
| Worker lifecycle | `make test-live-engineering` | Create worker, ready workspace/network, sidecar start, safe noop call, delete worker | NOT RUN. The helper sets `ORCHESTRATOR_ROOT=/home/vlad/projects/codegen_orchestrator`; that path is absent, so Docker operations and cleanup cannot be trusted from this workspace. |
| Production scaffold | `make test-live-smoke` | Scaffold only from pinned GitHub source and record resolved commit | NOT RUN. Source and ref were verified, but GitHub App credentials and running services were unavailable. |
| Deploy/readiness | `make test-live-mega` | Dedicated deployment reaches a terminal application state and health endpoint responds | NOT RUN. No test server could be selected safely and no deployment credentials were available. |
| QA/non-LLM smoke | Deployed `/health` or `/v1/health` probe | Observable HTTP 200 over the deployed application | NOT RUN. It depends on a successful dedicated deployment. The current full-pipeline test has no separate QA queue assertion. |
| Cleanup proof | Fixture `finally`/teardown plus ownership-scoped residue queries | Zero owned containers, networks, workspaces, repositories, allocations and DB rows | RED by inspection. `cleanup_all` logs and suppresses cleanup errors; `cleanup_github_repo` does not check the subprocess result; queue cleanup deletes shared streams rather than only owned messages. This cannot prove zero residue. |

## Safe diagnostic excerpt

```text
missing /home/vlad/projects/codegen_orchestrator
missing /home/vlad/projects/service-template
docker compose ps: no service rows
GITHUB_APP_ID=absent
GITHUB_APP_PRIVATE_KEY=absent
GITHUB_APP_INSTALLATION_ID=absent
HETZNER_API_TOKEN=absent
HCLOUD_TOKEN=absent
ANSIBLE_VAULT_PASSWORD=absent
SECRETS_ENCRYPTION_KEY=absent
```

`docker compose ps` also reported unset required Compose variables. Values are intentionally not
included here.

## Cleanup and ownership

No live test identifier was allocated and no external mutation command was run. Therefore this
attempt created no containers, networks, workspaces, GitHub repositories, deployments, port
allocations or test DB rows. Nothing was intentionally retained. Existing resources were not
enumerated or modified because their ownership could not be proved.

The broad `make test-live-clean` target was not run. It matches all historical names with generic
prefixes and visits every configured server, which is not an acceptable substitute for cleanup of
a recorded run manifest.

## Blockers and follow-up findings

1. Provide a dedicated Stage 7 live environment with a running stack, required credentials, and an
   explicitly designated test server. Credential presence must be checked without exposing values.
2. Make the live harness derive its repository root instead of using
   `/home/vlad/projects/codegen_orchestrator`.
3. Make cleanup failures fail the run and verify residue from an ownership manifest. Do not delete
   shared Redis streams or scan/delete resources by a generic historical prefix.
4. Add an explicit QA/non-LLM assertion after deploy, either through the QA queue contract or a
   documented health-only Stage 7 contract.

The code findings must be tracked as separate Pipeline cards in `Ideas`. The worker CLI exposes no
card-creation operation, so they are included in the blocked worker report for secretary/PO triage.
