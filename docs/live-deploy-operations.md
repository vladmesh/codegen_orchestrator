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

## Drain unreconcilable temporary QA access

Use the operator drain only for one of these records:

- a non-revoked legacy record with no `target_base_url`, which the current
  target-backed reconciler cannot operate; or
- a complete target-backed record whose bounded reconciler has already persisted
  `status=revoke_failed` and `escalated_at`.

Ordinary current-format `granting`, `granted`, or `revoking` records are not
eligible. Let the reconciler prove the remote revoke or exhaust its configured
bound first. Then call the audited API as a resolved administrator:

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  --header "X-Internal-Key: ${INTERNAL_API_KEY}" \
  --header "X-Telegram-ID: ${ADMIN_TELEGRAM_ID}" \
  --header 'Content-Type: application/json' \
  --data '{"reason":"operator_drain"}' \
  "${API_BASE_URL}/api/temporary-access-grants/${GRANT_ID}/drain"
```

The response is the settled record: `status=revoked`, a non-null `revoked_at`,
and `revoke_reason=operator_drain`. The same command is idempotent. Its durable
`WorkAdmissionAudit` has `subject=temporary_access_drain`, the grant id as
`reference_id`, the resolved actor, and the before/after statuses.

This command records the operator's acceptance of unproved remote cleanup. It
does not establish that the QA identity is absent on the generated service.
Investigate or clean the remote target separately when that fact is required.
Never replace this action with a direct SQL status update.

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

### Failed workspace ensure (`workspace_ensure_failed`)

When ensure-workspace fails for an ACTIVE project (for example after story
teardown cleared `workspace_ready` and the clone failed), the scaffolder records
`scaffold_error` on the project and the scheduler stops re-running ensure. On its
next tick dispatch admission parks every story that has a todo task: the story
and task show the reason `workspace_ensure_failed`, the detail is
`Workspace ensure failed, so engineering cannot start: <recorded error>`, the
attempt id starts with `ws-`, and the owner and administrators each get one
notice. The task is not refused again while it is parked.

Fix the cause the detail names (repository access, name, disk), then click
`Retry infrastructure attempt` on the story, which is the same
`POST /api/stories/{story_id}/retry-infrastructure-attempt`. For this reason it
also removes `scaffold_error`, and only that key, from `project.config` in the same
transaction. The scheduler's next tick runs ensure again and dispatches the task
once `workspace_ready` is set. If ensure fails again, the story parks again with a
new attempt id and one new notice; retry again after fixing the cause. Never edit
`project.config` by hand to clear `scaffold_error`. When several stories of the
project were parked, each keeps its own park and needs its own retry.

## Reconcile managed deploy targets

Provisioning success has one internal commit point:
`POST /api/servers/{handle}/provisioning/finalize`. Infra-service sends the
reserved attempt/episode fence, the row identity observed before proof, the
generated key identity actually used for login and software proof, completion
labels and the QA receipt. The API validates all of them under the server-row
lock before writing the encrypted key, labels, receipt, incident settlement,
episode reset and READY together. Do not repair a partial success with key,
label, status or receipt PATCHes; a conflict means an operator edit or newer
episode won and must be observed as current state.

Infra-service retains the exact finalization command as one encrypted,
delivery-bound Redis value for 24 hours before sending it. A connection reset,
timeout or unknown 5xx leaves the stream entry pending. Reclaim retries only
that command; it does not rerun provisioning or rotate the key. The encrypted
command is removed after the definitive result is published and acknowledged.
A missing, expired or corrupt replay value is a `provisioning_failed` incident
and leaves the target non-admitting. Do not delete
`provisioner:finalization-replay:*` values to retry provisioning; repair the
incident and submit a new explicit provisioning request only after observing
the current server row and host credential.

The production deploy's `Reconcile managed deploy targets` step runs, after the
services are healthy and against the exact deployed SHA:

```bash
docker compose exec -T infra-service \
  python -m src.provisioner.target_readiness --revision "$DEPLOYED_SHA"
```

It prints one JSON line per managed server, whatever its status:

- `ready` — receipt written for the current QA target profile.
- `not_ready` — readiness failure recorded; admission refuses the row. An
  admitting row is parked as `error` and gets its status back when proved; any
  other status (`unreachable`, `reserved`, a provisioning `error`) is left alone.
- `in_progress` — provisioning owns the row (`pending_setup`, `provisioning`,
  `force_rebuild`); it was not reconciled.
- `unhandled` — a managed row whose software phase is not complete, or one that
  changed or cannot be addressed; it was not reconciled.
- `superseded` — the row's key, user or address changed while it was being
  proved, so no verdict was recorded.
- `unrecorded` — the verdict could not be written.

Only `ready` and `not_ready` are successes; any other outcome fails the deploy
step. Finish or repair provisioning for an `in_progress` or `unhandled` row, and
re-run the single-target command below for a `superseded` one. The command runs
only the login, privilege and retrofit playbooks: no reinstall, no firewall
change, no QA or stand run.

A `not_ready` target has one active `target_not_ready` incident, separate from
any `provisioning_failed` episode, whose details carry the failed `phase`, and
the row's `target_readiness_failure_phase` names it too:

- `ssh_key_missing` / `ssh_key_invalid` — the stored administrative key is
  absent or does not parse. Supply valid operator material with
  `PATCH /api/servers/{handle}` `{"ssh_key": "<unencrypted OpenSSH private key with final newline>"}`;
  a refused key returns `ssh_key rejected: <reason>` and changes nothing.
- `admin_login` — the login run failed or timed out as `servers.ssh_user`.
- `privilege_preflight` — the login succeeded, and the separate privilege run
  failed or timed out reaching root through non-interactive `sudo`/`become`.
  Nothing on the target was changed.
- `qa_identity_role` / `qa_identity_proof` — the role could not be applied, or
  its proof refused the seat (the incident detail names what it found).

After repairing, reconcile that one target, which records the verdict the same
way and is safe to repeat:

```bash
docker compose exec -T infra-service python -m src.provisioner.qa_identity_retrofit "$HANDLE"
```

Never write `qa_target_version` or labels by hand: only the readiness endpoint
records a receipt, and only for the current profile. A story parked with a QA
harness blocker (`qa_target_profile_stale`, `qa_probe_unavailable`,
`server_unavailable`, `qa_executor_unavailable`, `qa_identity_unreadable`) is
recovered after reconciliation with `POST /api/stories/{story_id}/recheck-qa`.

## Log in the production subscription executor profiles

Worker-manager reads the dedicated Claude and Codex host-session profiles in place
every 30 seconds through its read-only mounts (`/host-claude`, `/host-codex`) and
publishes their login state, refresh-material state and stored expiry facts in
`GET /api/work-admission/executor-diagnostics` and the admin Settings
`Executor diagnostics` card. It never runs a CLI, contacts a provider or refreshes
a token. A logged-out, missing-refresh, unusable or expired profile is
`unavailable` and refuses new starts; a refresh credential whose stored expiry is
at or inside 24 hours is `degraded` and still admits starts until it expires;
malformed or contradictory metadata is `unknown`. An access/session expiry alone
never degrades a profile that holds refresh material. A Codex profile whose
`auth.json` is not in the ChatGPT subscription `auth_mode` is unavailable. The
first of those states sends one Telegram alert to every administrator, retried
with backoff until every administrator received it; further changes during the
same unhealthy stretch (for example expiring to expired) update the card without
another alert, and only a healthy observation closes the executor's episode so a
later regression alerts again. `Host-session profile was being refreshed` means
a worker's CLI was rewriting `auth.json` during the read; it is re-read on the
next tick and never alerts.
Worker-manager needs `TELEGRAM_BOT_TOKEN` and `INTERNAL_API_KEY` from `.env` to
deliver it.

Refresh tokens rotate: the CLI that refreshes receives a new refresh token and
the old one stops working at the provider. **Never run `claude` or `codex`
against a copy of either profile** — not to test it, not to probe quota. The copy
wins the rotation and the real profile, which workers use, is dead. Check a
profile only through the diagnostics endpoint or UI; repair it only by logging in
again into the real dedicated directory below. Neither CLI stores a refresh-token
expiry for an opaque refresh token, so the card shows `Not exposed by CLI` there;
the access/session expiry row is renewable while a refresh credential is present.

### Claude: paste-code login through a FIFO

`HOST_CLAUDE_DIR` is the dedicated profile (production: `/home/deploy/.claude-worker`),
never the operator's own `~/.claude`. The login prints an authorization URL and
then reads the pasted code from standard input; a FIFO lets the code arrive from a
second shell while the CLI keeps running. Both shells must be logged in as the
same user, the one that owns the profile, because both use the one fixed FIFO path
`$HOME/.claude-login.fifo`, which only that user can write.

**Shell 1**, from the deployment directory. The block refuses to start if the FIFO
path already exists, blocks while the CLI waits for the code, and cleans up and
fixes permissions itself when the CLI exits:

```bash
set -a; . ./.env; set +a                        # HOST_CLAUDE_DIR
install -d -m 0700 "$HOST_CLAUDE_DIR"
test ! -e "$HOME/.claude-login.fifo" && mkfifo -m 0600 "$HOME/.claude-login.fifo" \
  && { sleep 900 > "$HOME/.claude-login.fifo" & HOLD=$!; } \
  && docker run --rm -i --user "$(id -u):$(id -g)" \
       -e CLAUDE_CONFIG_DIR=/home/worker/.claude \
       -v "$HOST_CLAUDE_DIR":/home/worker/.claude \
       --entrypoint claude worker-base-claude:latest auth login < "$HOME/.claude-login.fifo"
kill "$HOLD" 2>/dev/null
test -p "$HOME/.claude-login.fifo" && rm -f -- "$HOME/.claude-login.fifo"
chmod 0700 "$HOST_CLAUDE_DIR" && chmod 0600 "$HOST_CLAUDE_DIR/.credentials.json"
```

If `test ! -e` stops the block, a previous attempt left the FIFO behind: confirm no
login is running, remove it with `rm -f -- "$HOME/.claude-login.fifo"`, and rerun.

Open the URL shell 1 printed and approve the login. **Shell 2**, as the same user,
delivers the code shown by the browser (nothing from shell 1 is needed):

```bash
printf '%s\n' '<code shown by the browser>' > "$HOME/.claude-login.fifo"
```

### Codex: device-code login

`HOST_CODEX_HOME` is the dedicated file-backed profile (production:
`/home/deploy/.codex-worker`), never the operator's live `~/.codex`. Device
authentication prints a URL and a one-time code and needs no standard input:

```bash
set -a; . ./.env; set +a                        # HOST_CODEX_HOME
install -d -m 0700 "$HOST_CODEX_HOME"
printf 'cli_auth_credentials_store = "file"\n' > "$HOST_CODEX_HOME/config.toml"
chmod 0600 "$HOST_CODEX_HOME/config.toml"
touch "$HOST_CODEX_HOME/.codegen-codex.lock"    # keeps an existing lock inode
chmod 0600 "$HOST_CODEX_HOME/.codegen-codex.lock"
flock "$HOST_CODEX_HOME/.codegen-codex.lock" \
  docker run --rm -it --user "$(id -u):$(id -g)" \
    -e CODEX_HOME=/home/worker/.codex \
    -v "$HOST_CODEX_HOME":/home/worker/.codex \
    --entrypoint codex worker-base-codex:latest login --device-auth
chmod 0600 "$HOST_CODEX_HOME/auth.json"
```

The login runs under the same `.codegen-codex.lock` that every Codex worker holds
for its whole CLI process (`flock` waits for a running worker to finish). Never
recreate that file with `install`, `cp` or `rm`: worker-manager joins the existing
inode, and until it can, it reports `Host-session profile was being refreshed`
(unknown, no alert, no refusal) instead of trusting a read that a worker could be
rewriting. Creating the lock in the login step is what lets the next diagnostics
tick report the new login as healthy.

Worker-manager refuses a Codex profile whose directory is not `0700`, whose
`auth.json` or `config.toml` is not `0600`, whose credential store is not
`file`, whose `auth.json` does not load as the pinned CLI's auth format, or whose
`auth_mode` is not the ChatGPT subscription mode.

### Restart and recheck

A login is observed on the next 30-second diagnostics tick. Restarting
worker-manager publishes immediately at startup:

```bash
docker compose restart worker-manager
sleep 5
docker compose exec -T api sh -c \
  'curl -fsS -H "X-Internal-Key: $INTERNAL_API_KEY" \
   http://127.0.0.1:8000/api/work-admission/executor-diagnostics' \
  | jq '.diagnostics[] | {executor, availability, reason_code, profile}'
```

Continue only when the repaired executor shows `availability: "available"` and
`profile.condition: "healthy"` (or the Settings card shows `Logged in` with a
`Present` refresh credential). The response and card carry no token, claim or
path; do not paste profile file contents anywhere to debug.

