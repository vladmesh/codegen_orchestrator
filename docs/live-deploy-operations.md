# Live deploy operations

Codegen-side operational notes for the live deploy path (mega E2E). Product
behaviour only. Host, server, and pipeline operations are intentionally not recorded
here.

## Rebuilding the deploy path

The deploy graph runs in its own compose service, `deploy-worker`. After changing
`services/langgraph/src/consumers/deploy.py` or
`services/langgraph/src/subgraphs/devops/`, rebuild and recreate `deploy-worker`
specifically:

```bash
docker compose up -d --build deploy-worker
```

Rebuilding `langgraph` alone leaves the old deploy image running, which silently
reproduces already-fixed deploy behaviour.

## Infrastructure port allocation

`POSTGRES_HOST_PORT` and `REDIS_HOST_PORT` are exact computed keys resolved from the
application allocations `postgres` and `redis`, never hardcoded. A missing or
ambiguous allocation raises `SecretResolutionError` and produces a visible failed
run; a redeploy reuses the stored allocations.

Fail-fast boundary: an unknown computed key, empty project context, invalid port, or
partial image URL must stop the deploy, not fall back to a default.

Known architectural risks: deploy always reserves `postgres` and `redis` allocations
even for a project that does not use them (correct for service-template 0.3.x; a
general deploy should read required infrastructure from an explicit project
contract). Re-allocating a missing module picks a freshly chosen server, so backend
and postgres could in principle land on different hosts. Target invariant: all
allocations of one application belong to one deploy target, unless the model gains
explicit support for distributed deploys.

## Typed env contract

Deploy resolves environment from a per-service `env.contract.yaml` by source type,
not from heuristics or an LLM. Source types:

- `user_secret` — provided by the user or an external owner
- `generated_secret` — created by the orchestrator
- `allocation` — taken from a server/application allocation
- `derived` — deterministically computed from other data
- `literal` — non-secret value from the template or spec

A repository without a contract is a failed run with a contract outcome — codegen is
not in production and no contract-less repos exist, so no LLM fallback classification
is kept. Typed deploy outcomes, distinguishable in `run.result`:
`waiting_for_user_secret`, `allocation_missing`, `environment_contract_invalid`,
`environment_resolution_failed`. Secret and non-secret maps stay separate until final
dotenv assembly (mixed only in the deployer). Design: `docs/plans/typed-env-contract-mvp.md`.

Migration state: schema + validator (codegen) and baseline fragments (template) →
env-usage extractor + CI gate → typed deploy resolution → legacy analyzer / LLM
classification removed. The code side of the migration is complete.

## Deploy and QA gotchas

- **Prod compose must publish the web port**, not just expose it. A container that is
  healthy on `localhost:8000/health` inside is unreachable from the host when the
  compose service has no `ports:` section — `docker ps` shows `8000/tcp` (expose
  without publish), and an external `curl host:PORT/health` returns 000, so QA times
  out. The published mapping (`${BACKEND_PORT}:8000`) plus a unique per-app
  `BACKEND_PORT` is what makes the app externally reachable.
- **A green internal health check can lie.** The orchestrator marks an application
  RUNNING via an internal probe at `{ip}:{app.ports[0]}/health`, which diverges from
  external reachability when the port is not published.
- **non-LLM QA is client-side.** It polls `{deployed_url}/health` and `/v1/health`
  for a 200 up to 420s (`live_harness.run_non_llm_qa`); it does not go through the
  qa-worker.
- **A port is allocated per module** (`allocations.py` writes `service_name = module`;
  infrastructure services are `postgres`/`redis`); the web module is `backend`.
- **Cleanup must not race an active deploy.** Live-harness cleanup has to cancel or
  await active deploy runs before deleting their resources; otherwise a teardown-race
  (repo/DB deleted while the deploy worker is still retrying an Actions run) masks the
  real error.

## Running the mega

```bash
make test-live-mega      # full pipeline; live LLM worker path needs a working agent credential
make test-live-clean     # always run after a live attempt
```

The mega runs only on explicit request. Live-run logs go to `.live-runs/`, debug
artifacts to `docs/e2e_results/`.
