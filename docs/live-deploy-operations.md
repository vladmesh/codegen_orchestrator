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

Rebuild and recreate `deploy-worker` after a deploy-path change so it runs the
current graph image.

## Infrastructure port allocation

`POSTGRES_HOST_PORT` and `REDIS_HOST_PORT` are exact computed keys resolved from the
application allocations `postgres` and `redis`, never hardcoded. A missing or
ambiguous allocation raises `SecretResolutionError` and produces a visible failed
run; a redeploy reuses the stored allocations.

Fail-fast boundary: an unknown computed key, empty project context, invalid port, or
partial image URL must stop the deploy, not fall back to a default.

Known architectural risks: deploy always reserves `postgres` and `redis` allocations
even for a project that does not use them. Under the pinned `codegen-product-kit`
this over-reserves: the kit's compose only defines `db` for a `backend` project, so a
`tg_bot`-only product gets a Postgres allocation it never opens. A general deploy
should read required infrastructure from an explicit project contract. Re-allocating a missing module picks a freshly chosen server, so backend
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
dotenv assembly (mixed only in the deployer).

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
make test-live-mega-noop                              # free full pipeline, no model call
make stand-run SUITE=mega-llm WORKER=codex QA=claude # one real coding/QA pair on the stand
make test-live-clean                                  # always run after a local live attempt
```

The mega runs only on explicit request. Live-run logs go to `.live-runs/`, debug
artifacts to `docs/e2e_results/` (local, gitignored).

### Cleanup target admission

Both standalone cleanup and write-ahead manifest recovery select remote targets with the same
fail-closed provisioning policy used by the product: the API row must have `is_managed=true` and a
positive provider ID present in `PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS`. Unrelated inventory rows are logged
and skipped before SSH-key retrieval or SSH. Once a target is admitted, a missing key, malformed
connection data, failed residue scan or failed teardown is an error; key absence is never treated as
proof of cleanliness. An owned manifest with no admissible target is also an error.

## Story-owned worker rollout precondition

Deploy worker-manager before enabling the matching scheduler and engineering/QA
consumers, then read `GET /api/introspect/worker-lifecycle`. The single rollout
precondition is `ownerless_project_locks.count == 0`. Terminal legacy workers
that still have a `story:workers` binding are drained automatically once the
matching scheduler is enabled; active or ambiguous ownerless locks are not
guessed or deleted and must be resolved before the rollout continues. The
diagnostic reports identifiers, counts, oldest known age, and unknown-age counts
only; it exposes no environment values or credentials.

Released run-owned storyless workers intentionally appear in
`ownerless_project_locks` while active. With consumers disabled, let that work
finish through its normal delete-on-success path (or apply the normal operator
cancellation decision) before requiring the rollout count to reach zero.

Resolve an identifiable legacy lock with the canonical removal path, never by
deleting Redis keys directly:

1. Keep the new scheduler and engineering/QA consumers disabled. For each
   project named by `ownerless_project_locks.identifiers`, read
   `workspace:lock:<project>` and record its exact worker id.
2. Read that worker through `GET /api/introspect/workers/<worker_id>`. Use only
   the exact returned `container_id` for a read-only `docker inspect`, then read
   its existing `com.codegen.worker.id` and `com.codegen.project.id` labels.
   Continue only when the Redis holder, introspection result, and both labels
   name that same worker and project. Do not select by a name prefix. Missing
   metadata, a mismatched label, multiple candidates, or unavailable Docker
   evidence is ambiguous and blocks the rollout.
3. If the worker is still doing intended work, let it finish or use the normal
   operator cancellation decision first. Once it is safe to stop, call
   `DELETE /api/introspect/workers/<worker_id>`. This invokes worker-manager's
   canonical deletion and owner-fenced lock release.
4. Re-read the lock and lifecycle diagnostic. Continue the rollout only after
   the exact lock is absent and `ownerless_project_locks.count` is zero.

Legacy age is measured from existing worker metadata when present, otherwise
from the Docker container's `Created` timestamp. A record with neither source
increments `unknown_age_count`; no age is fabricated.

Rejected pre-container creates retain their terminal status and error for five
minutes so callers can observe the refusal, after which Redis expires both.

## Recover a pre-agent infrastructure refusal

A parked story is already complete when it appears in human review: the park
transaction wrote both evidence copies and the owed owner and administrator
notices together. Either notice may still be in delivery; the owner-notification
supervisor settles each audience on its own, and recovering first does not cancel
the administrator notice. Do not wait for delivery before recovering.
The story's `owner_notification.admin_state` is `delivered` only when Telegram
accepted every configured administrator; `owed` with an `admin_detail` such as
`partial: Telegram accepted 1 of 2 configured administrators` is still being
retried (a reached administrator may get a repeat), `abandoned` gave up after
the bound, and `unaddressable` means no user has `is_admin` set.

On the admin story detail page, confirm the task and story show the same typed
infrastructure reason, then click `Retry infrastructure attempt` once. The UI
calls `POST /api/stories/{story_id}/retry-infrastructure-attempt` with the exact
task, refused attempt, and reason. The response is `retried`, or
`already_retried` when that audit was already applied. The transaction settles
the refused Run fence, preserves `current_iteration`, records the legal task
status hops, clears only the matching park evidence, and restarts the story; the
scheduler creates the fresh attempt on its next tick.

Do not PATCH `current_iteration`, sequence task transitions, or start the story
manually. The action returns a typed 409 without partial changes when the reason
is stale, either row left human review, the park is not infrastructure-owned, or
the refused Run no longer matches. Resolve that discrepancy before retrying.
