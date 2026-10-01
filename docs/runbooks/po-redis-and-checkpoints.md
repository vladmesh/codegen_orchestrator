# PO Redis and checkpoint production runbooks

These procedures are operational runbooks, not default architectural context.
Read them only when performing or reviewing the corresponding production migration.

Back to [Secrets Management Architecture](../SECRETS.md).

## Production PO Redis upgrade

This is a separate authorized production operation after merge/release, not an
action performed by this code card. Reserve an exclusive maintenance window
against Deploy, schedulers, independent processes and manual restarts. Use the
existing key that decrypts project secrets/checkpoints. Old code cannot read
converted PO payloads; never resume an old producer/consumer after conversion.
This procedure owns the ordering for both Redis and checkpoint upgrades. Complete
the checkpoint storage steps below in this same window before resuming PO; there
is one Deploy, one final client stop and one resume, including on retries.

The invariant is: finish old-image delivery and writers before the final drain
check; keep admission and Redis write fences across Deploy restarts; keep Deploy's
dependencies available; convert only after complete successful Deploy, released
image readback and full client quiescence. Preserve original data and fences on
failure. `up --no-deps` resumes a completed upgrade; it cannot finish missed
migrations, seeding or target reconciliation.

##### Shared stage and dependency map

| Boundary | Required available services / work | Services the operator may stop |
|---|---|---|
| Before backup/dispatch | Old API and Redis for drain/preflight; old LangGraph until input and active turns finish; verified old Telegram for delivery | Stop system producers after active work settles; gracefully finish Telegram handlers; stop LangGraph before the final proactive drain |
| Backups complete, before dispatch | DB and Redis; verified backups; stable provisioning preflight; independent admission fence and Redis WRITE pause | All normal clients stopped; start only old API for the read-only preflight if needed |
| Switch all-service `up` through promotion, migrations, API health/key read and seed | API, DB and Redis; API must stay available through Reconcile too | Stop only `telegram_bot langgraph engineering-worker deploy-worker architect qa-worker worker-manager worker-broker scaffolder`; keep `api infra-service` and all three schedulers available |
| Switch scheduler recreate with `--wait` | All three schedulers, API, DB and Redis; infrastructure startup replay must have no provisioning work | Keep schedulers running while Compose waits; use the same nondependent stop list above |
| Reconcile and remaining workflow steps | `infra-service`, API, DB, Redis and target SSH/Ansible; every managed target gets a recorded verdict | After Switch has completed successfully (including scheduler wait), the three schedulers may stop; API and infra-service remain available until the entire run completes successfully |
| Successful complete Deploy and readback | DB and Redis; all normal/independent clients stopped | Stop the entire client list in step 2, including API and infra-service; kill deferred normal connections before unpause; convert both stores |
| Resume | Both keyed dry-runs, direct storage counts and Redis persistence verification passed; API available for LangGraph config | Start API, then released protected LangGraph, then Telegram and the other clients; remove admission fence only after validation |

This follows [Switch and Reconcile](../../.github/workflows/deploy.yml): promotion
follows all-service `up`, then API Alembic, local `/health`, key readability,
`seed_system_configs.py`, scheduler recreate/wait, and production
`src.provisioner.target_readiness`. Keep DB/Redis and runtime mounts available
throughout. Do not stop API/infra-service at the first `up`, stop schedulers during
their wait, skip readiness/reconciliation, or release paused writes to get green.

##### Stable provisioning preflight

Arrange a freeze, independent of container restarts, on external API admissions
(including admin/internal provisioning requests), direct DB writers, provider
account inventory/IP/hostname changes, manual target changes and provisioning
policy/credential changes in GitHub secrets. Keep the existing key and the exact
Time4VPS allowlist unchanged by Deploy. This is an exclusive maintenance boundary,
not a new provisioning policy. Finish all active provisioning/Ansible work and
delivery/result handlers before stopping old producers. Count pending/unread
work in `provisioner:queue` / `infrastructure-workers` and `provisioner:results` /
`scheduler-consumers` and `telegram-bot` with the group check in step 1; all must be zero.
Pub/sub has no durable backlog: establish that its handlers are idle too.

After old writers settle, the following count-only preflight must have every
count zero. It deliberately excludes *all* `pending_setup`, `provisioning` and
`force_rebuild` rows, including unauthorized rows. Every managed row must satisfy
`target_readiness_reconcilable` and have an address. Every allowlisted provider ID
must already have exactly one managed, completed database row. Every provider
inventory entry must match a Time4VPS row, identity and management flag; every
row must match provider inventory unless it meets the settled-history rule below.
Settle drift using the existing supported procedures before this window; do not
patch statuses, remove authorization or hide a target to pass these checks.

An already-`unreachable` Time4VPS row with a valid provider ID, `is_managed=false`,
and an ID outside the allowlist is settled history when that ID is absent from
provider inventory. It grants no provisioning or readiness authority and needs
no provider target to reconcile; retain the row. A managed or allowlisted row,
any other status, a malformed ID, duplicate rows, or any provider-present
identity/management mismatch remains blocking drift. Provider inventory entries
without a row and allowlist entries without a completed managed row also block.

After initializing the dedicated operator script below, run this through the
verified old scheduler image, with its existing API credentials,
before backup and again immediately before dispatch (start only the old API if
step 2 stopped it). Keep output restricted outside the checkout:

```bash
"${COMPOSE[@]}" run --rm --no-deps --pull never scheduler-infrastructure \
  python -m src.maintenance_preflight \
  > "$PO_REDIS_BACKUP_DIR/provisioning-preflight.log" 2>&1
```

The scheduler's `get_servers(status=PENDING_SETUP)` calls authenticated
`GET /api/servers/?status=pending_setup`; the preflight lists all rows with
`GET /api/servers/` (no pagination). `InternalAPIClient` injects `X-Internal-Key`
from the container environment; API `require_internal_or_admin` authorizes it and
`list_servers` queries PostgreSQL. The retry then GETs each individual row and
checks `provider_operation_is_authorized` again before PUBLISH. A rejected HTTP
request is a failure, never an empty inventory. Provider inventory is read through
the existing encrypted API-key endpoint and Time4VPS `GET /server`; no credential
or row is exported to notes.

With these conditions frozen, scheduler `retry_pending_servers` returns before
opening a Redis publisher. `runtime.run_workers` writes its readiness file before
awaiting the worker loops; Redis writes inside loops can wait without preventing
readiness. Server sync can write PostgreSQL independently of the Redis pause:
the matching frozen inventory prevents discovery of new managed `pending_setup`
rows, and zero scheduled rows excludes its provisioning transitions. Health checks
change metrics/operational statuses, not completion labels or provisioning intent;
paused XREADGROUP prevents result consumers from applying fresh provisioning
results. API startup creates a lazy Redis client, `/health` is local, migrations
use PostgreSQL, and the seeder's system-config and paid-work-control routes use
PostgreSQL. Reconcile lists managed rows, reads the decrypted administrative key,
runs SSH login/privilege/QA retrofit plays, and records labels/readiness/incident
transactions through HTTP/DB; it has no Redis write dependency. Sources:
[scheduler startup](../../services/scheduler/src/infrastructure.py),
[retry](../../services/scheduler/src/tasks/provisioner_trigger.py),
[readiness](../../services/scheduler/src/runtime.py),
[server sync](../../services/scheduler/src/tasks/server_sync.py),
[API dependencies](../../services/api/src/dependencies.py),
[server routes](../../services/api/src/routers/servers.py),
[seeder](../../scripts/seed_system_configs.py),
[reconciliation](../../services/infra-service/src/provisioner/target_readiness.py) and
[retrofit](../../services/infra-service/src/provisioner/operations.py).

Redis [WRITE pause](https://redis.io/docs/latest/commands/client-pause/) also
defers PUBLISH and XREADGROUP. An authorized pending server would therefore hang
startup before readiness and fail Deploy. A zero snapshot alone is insufficient:
hold the freeze through both all-service startup and scheduler recreate. Recheck
the counts with the released image once the seeded API is available and after
Switch; compare the effective allowlist/key with the restricted before-render
without printing them. The frozen conditions exclude the race before the check,
rather than attempting to unpause to repair it. On drift, inability to hold the
freeze, active work, failed reads or readiness timeout, retain the admission/write
fences and backups and abort conversion. Wait for active Deploy commands to settle;
do not interrupt Alembic or Ansible. A supported window cannot proceed until the
preconditions can be established; fix provisioning separately, never skip it.

Execute every strict shell block below in a dedicated Bash script or subshell,
not the interactive control pane. Keep configs, dumps, old drain logs and copies
of Redis `/data` outside the checkout, in an operator-owned 0700 directory with
0600 files. Never attach them to reports. Set `PO_REDIS_BACKUP_DIR` to an absolute,
external path, `PO_REDIS_RELEASE_SHA` to the full released main SHA, and retain
the deployment environment's complete `DEPLOY_PATH`. First install the reviewed helpers through
the later PO [backup operation](../DEPLOY.md#later-po-operation-install-and-prove-production-nightly-backup).
Run every host block, including raw Docker readback, in the owning user's login session. On h01o
that is vlad/UID 1001, not deploy or an administrator's default Docker context. Set `BACKUP_POLICY`
to `/home/vlad/.config/codegen-orchestrator/backup.env`; its non-secret configuration supplies
the checked identity/runtime and full Compose chain, including project `codegen_orchestrator`,
`/home/vlad/codegen-h01o.override.yml` and the current digest override last. The installed client
explicitly binds `unix:///run/user/1001/docker.sock` and refuses a missing/mismatched runtime/socket:

```bash
  set +x
  set -euo pipefail
  umask 077
  : "${DEPLOY_PATH:?}"
  : "${BACKUP_POLICY:?Reviewed non-secret policy of the owning installation}"
  : "${PO_REDIS_RELEASE_SHA:?}"
  : "${PO_REDIS_BACKUP_DIR:?}"
  set -a
  . "$BACKUP_POLICY"
  set +a
  test "$(id -un)" = "$BACKUP_USER"
  test "$(id -u)" = "$BACKUP_UID"
  test "$DEPLOY_PATH" = "$COMPOSE_DIR"
  BACKUP_DOCKER=(/usr/local/libexec/backup-db-rootless.sh docker)
  test "${PO_REDIS_BACKUP_DIR:0:1}" = /
  cd "$DEPLOY_PATH"
  case "$PO_REDIS_BACKUP_DIR/" in "$PWD/"*) exit 1 ;; esac
  install -d -m 0700 "$PO_REDIS_BACKUP_DIR"
  po_compose_refresh() {
    read -r -a po_compose_args <<< "$COMPOSE_ARGS"
    COMPOSE=("${BACKUP_DOCKER[@]}" compose --project-directory "$COMPOSE_DIR" "${po_compose_args[@]}")
  }
  po_compose_refresh
  CHECKPOINT_RELEASE_SHA="$PO_REDIS_RELEASE_SHA"
  checkpoint_backup_dir="$PO_REDIS_BACKUP_DIR/checkpoints"
  install -d -m 0700 "$checkpoint_backup_dir"
  "${COMPOSE[@]}" config --format json > "$PO_REDIS_BACKUP_DIR/compose-before.json"
  python3 scripts/service_release.py readback \
    --compose-config "$PO_REDIS_BACKUP_DIR/compose-before.json" \
    --record deployed-service-images.json --deploy-path "$PWD"
```

Use one dedicated operator script for the staged commands so its arrays, fence
identity/deadline and paths persist. `COMPOSE_ARGS` is whitespace-separated, as
in Deploy; configured paths cannot contain spaces. Verify unchanged Redis image,
service configuration and data volume in the release before dispatch, so its
all-service `up` need not recreate Redis. If that cannot be proved, abort this
supported window. `CHECKPOINT_DATABASE_URL` must select the intended `langgraph`
schema through `options=-c%20search_path%3Dlanggraph`, as Compose does. Verify the
existing encryption key will remain unchanged in both maintenance containers.

1. Drain with the released old image, before Deploy. Stop system-event producers
   (`scheduler-pipeline`, `scheduler-infrastructure`, `scheduler-maintenance`,
   `engineering-worker`, `deploy-worker`, `architect`) after their active work
   settles. Pause API admissions and other independently launched producers.
   Keep old `langgraph` running to finish queued/pending PO turns. Send SIGTERM
   to the old Telegram container, allowing python-telegram-bot to stop polling
   and finish its queued handlers, reply waiters and deliveries. Do not force
   kill it or use a short Compose stop timeout. Wait for its graceful exit in
   the foreground, then confirm PO input pending=0 and unread=0 using only group
   counts/cursors. Telegram updates still at Telegram must remain there; never
   set `drop_pending_updates`, FLUSH, XTRIM, XDEL or manual XACK to clear a drain.

   In a dedicated operator script with the same `COMPOSE` array:

   ```bash
   "${COMPOSE[@]}" stop scheduler-pipeline scheduler-infrastructure scheduler-maintenance \
     engineering-worker deploy-worker architect
   po_old_bot_id=$("${COMPOSE[@]}" ps -q telegram_bot)
   test -n "$po_old_bot_id"
   "${BACKUP_DOCKER[@]}" kill --signal SIGTERM "$po_old_bot_id" > /dev/null
   test "$("${BACKUP_DOCKER[@]}" wait "$po_old_bot_id")" = 0
   ```

   Use this read-only group check through the verified old bot digest. It emits
   only counts, compares the actual released names, empty PEL and each
   `last-delivered-id` with `last-generated-id`; it reads no entry bodies. An
   absent stream is empty; an existing nonempty stream must have all its released
   groups. Unknown groups/types refuse. The same function checks provisioning
   delivery/results after those handlers have settled:

   ```bash
   po_old_group_check() {
     "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python telegram_bot -c '
   import asyncio, json, os, sys
   import redis.asyncio as redis
   from shared.queues import (PO_INPUT_QUEUE, PO_CONSUMER_GROUP, PO_PROACTIVE_QUEUE,
       PO_PROACTIVE_GROUP, PROVISIONER_QUEUE, INFRA_GROUP, PROVISIONER_RESULTS,
       SCHEDULER_CONSUMER_GROUP, TELEGRAM_BOT_GROUP)
   async def check():
       client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
       specs = {PO_INPUT_QUEUE: {PO_CONSUMER_GROUP}, PO_PROACTIVE_QUEUE: {PO_PROACTIVE_GROUP},
           PROVISIONER_QUEUE: {INFRA_GROUP},
           PROVISIONER_RESULTS: {SCHEDULER_CONSUMER_GROUP, TELEGRAM_BOT_GROUP}}
       counts = {"streams": 0, "groups": 0, "pending": 0, "unread_groups": 0, "invalid": 0}
       try:
           for key in sys.argv[1:]:
               expected = specs[key]
               kind = await client.type(key)
               if kind == "none":
                   continue
               if kind != "stream":
                   counts["invalid"] += 1
                   continue
               info = await client.xinfo_stream(key)
               groups = await client.xinfo_groups(key)
               names = {g["name"] for g in groups}
               counts["streams"] += 1
               counts["groups"] += len(groups)
               counts["invalid"] += int(bool(names - expected)
                   or (info["length"] > 0 and names != expected))
               counts["pending"] += sum(g["pending"] for g in groups)
               counts["unread_groups"] += sum(
                   g["last-delivered-id"] != info["last-generated-id"] for g in groups)
           print(json.dumps(counts, sort_keys=True))
           return int(any(counts[k] for k in ("pending", "unread_groups", "invalid")))
       except Exception:
           print(json.dumps({"group_check_failed": 1}))
           return 1
       finally:
           await client.aclose()
   sys.exit(asyncio.run(check()))
   ' "$@"
   }
   po_old_group_check po:input
   # Finish active PO turns, graph/tool calls and reminder-poller work, then:
   "${COMPOSE[@]}" stop langgraph
   test -z "$("${COMPOSE[@]}" ps --status running --status restarting -q langgraph)"
   # Recheck input after the last writer stopped; a racing reminder may add work.
   po_old_group_check po:input
   ```

   Empty input PEL alone does not prove no graph/tool invocation is in flight;
   establish that those turns and checkpoint writes completed before stopping.
   Every LangGraph replica and independent proactive/reminder producer must now
   be stopped. If the post-stop input check fails, keep old images and restore
   only the old consumers needed to deliver/settle that work, stop them again,
   and repeat this boundary before backup or dispatch. Preserve future reminders
   and notice obligations. Do not fire them early to empty the input.

   Only now drain proactive work with a one-off process from that same old
   `telegram_bot` digest, using the released
   `RedisStreamClient.consume` and `src.proactive.process_proactive_entry`, with
   `auto_ack=False`, `claim_pending=True`, `pending_timeout_ms=0`. Initialize
   `telegram.Bot` from the container's existing `TELEGRAM_BOT_TOKEN`; do not start
   an Application/getUpdates poller. This preserves addressed delivery, bounded
   retry/exhaustion/admin notice and ACK behavior, and admits no new Telegram
   input. A concrete drain entry point is:

   ```bash
   "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python telegram_bot -c '
   import asyncio, os
   from telegram import Bot
   from shared.redis import RedisStreamClient
   from shared.queues import PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP
   from src.proactive import process_proactive_entry
   async def drain():
       client = RedisStreamClient(os.environ["REDIS_URL"])
       await client.connect()
       await client.ensure_consumer_group(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP)
       async with Bot(os.environ["TELEGRAM_BOT_TOKEN"]) as bot:
           reader = client.consume(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, "maintenance-old-bot",
               block_ms=1000, auto_ack=False, claim_pending=True, pending_timeout_ms=0)
           try:
               async for message in reader:
                   if message is not None:
                       await process_proactive_entry(bot, client, message)
                   info = await client.redis.xinfo_stream(PO_PROACTIVE_QUEUE)
                   groups = await client.redis.xinfo_groups(PO_PROACTIVE_QUEUE)
                   group = next(g for g in groups if g["name"] == PO_PROACTIVE_GROUP)
                   if group["pending"] == 0 and group["last-delivered-id"] == info["last-generated-id"]:
                       break
           finally:
               await reader.aclose()
       await client.close()
   asyncio.run(drain())
   ' > "$PO_REDIS_BACKUP_DIR/old-proactive-drain.log" 2>&1
   ```

   Immediately before backup/image switch, with every producer still stopped,
   require successful final counts:

   ```bash
   po_old_group_check po:input po:proactive provisioner:queue provisioner:results
   ```

   Both PO streams must have `pending=0`, `unread_groups=0`, `invalid=0` with their
   released groups (`po-consumer`, `tg-bot-proactive`); provisioning groups must
   also be drained. Any late proactive entry is still deliverable by this old
   one-off bot: repeat its drain and the final check before switching. Future
   reminders and durable notice obligations are retained, never fired early or
   expired to pass the check. Retained response streams are preserved too;
   finish every old bot waiter before replacing its image. A blocked audience,
   notice lookup or pending handler must be resolved with the old supported
   consumer, not a new recovery subsystem or forced acknowledgement.
2. With all Redis clients quiesced, take a consistent, verified backup. Stop
   Redis, copy the complete `/data` directory (Redis 7 multipart AOF directory,
   manifest, any RDB, not just a guessed `appendonly.aof` filename), and verify
   isolated restore with the released old image/key before proceeding. Keep the
   corresponding database/checkpoint backup in the same window. Capture only counts and
   backup digests in operation notes:

   ```bash
   "${COMPOSE[@]}" stop api langgraph telegram_bot scheduler-pipeline scheduler-infrastructure \
     scheduler-maintenance engineering-worker deploy-worker architect qa-worker worker-manager \
     worker-broker infra-service scaffolder
   "${COMPOSE[@]}" stop redis
   po_redis_id=$("${COMPOSE[@]}" ps --all -q redis)
   test -n "$po_redis_id"
   install -d -m 0700 "$PO_REDIS_BACKUP_DIR/before-data"
   "${BACKUP_DOCKER[@]}" cp "$po_redis_id:/data/." "$PO_REDIS_BACKUP_DIR/before-data/"
   "${COMPOSE[@]}" up -d --no-deps --no-build --pull never redis
   BACKUP_DIR="$checkpoint_backup_dir" BACKUP_KIND=maintenance BACKUP_CONTOUR=production \
     BACKUP_LABEL="before-upgrade-$PO_REDIS_RELEASE_SHA" \
     /usr/local/libexec/backup-db-rootless.sh backup
   ```

   Account for every independent worker/Redis client before asserting quiescence.
   Backups contain historical plaintext even if the current dataset looks clean.
   Verify the database dump with the installation's isolated restore procedure
   too. Keep both verified backups and the unchanged deployed key available to
   the recovery operator. Old dumps/WAL archives/replicas and pre-RAG-drop copies
   remain restricted secret-bearing artifacts. Do not restore over production or
   delete restricted old artifacts here.
   Install the reviewed shared helper through the later PO [backup operation](../DEPLOY.md#later-po-operation-install-and-prove-production-nightly-backup)
   first. Record its generated archive path, exact bytes and `archive_list_exit=0`; a private
   temporary dump is verified before publication. Maintenance names are outside nightly retention.
   Preserve the archive and unchanged key even after conversion; historical contents are not scrubbed.
3. Dispatch the actual released Deploy from the authorized control shell:

   Fence external admissions independently of containers (the workflow restarts
   the API/proxy too), and pause Redis writes across the restart gap. In the host
   operator script record the Redis container ID/start time and the UTC deadline
   of a two-hour `CLIENT PAUSE WRITE` lease, then:

   ```bash
   po_redis_id=$("${COMPOSE[@]}" ps -q redis)
   test -n "$po_redis_id"
   po_redis_started=$("${BACKUP_DOCKER[@]}" inspect --format '{{.State.StartedAt}}' "$po_redis_id")
   po_pause_deadline=$(( $(date -u +%s) + 7200 ))
   "${COMPOSE[@]}" exec -T redis redis-cli CLIENT PAUSE 7200000 WRITE
   po_assert_write_fence() {
     test "$("${COMPOSE[@]}" ps -q redis)" = "$po_redis_id"
     test "$("${BACKUP_DOCKER[@]}" inspect --format '{{.State.StartedAt}}' "$po_redis_id")" = "$po_redis_started"
     test "$(date -u +%s)" -lt "$((po_pause_deadline - 600))"
   }
   po_assert_write_fence
   ```

   Administrative/count reads remain available; no restarted PO producer can
   XADD/SET/ZADD before the post-Deploy stop. Redis must not restart/recreate and
   the lease must not expire during this step. Record and verify those facts,
   never assume them from workflow success. Check before/after each workflow
   boundary and leave at least ten minutes to settle/stop clients. If more time
   is required, verify the same identity and renew WRITE pause *before* the old
   deadline, recording a new deadline; renewal must not unpause deferred writes.
   If identity changes or the lease lapses, retain the external fence and original
   data, stop clients unused by Deploy, let active migration/Ansible commands settle,
   and abort for an approved drain/restore decision. Reasserting a pause does not
   prove no writes occurred during a gap.

   ```bash
   gh workflow run deploy.yml --repo vladmesh/codegen_orchestrator --ref main \
     -f environment=production -f revision="$PO_REDIS_RELEASE_SHA"
   ```

   Record its run ID. Deploy rewrites `.env`, switches checkout/release records
   and calls all-service `up -d --remove-orphans --no-build --pull never`, so the
   earlier stops do not survive it. As soon as Switch runs that `up`, reconstruct
   the full current Compose array and stop only clients unused by Deploy:

   Switch first takes another verified, protected PostgreSQL archive, before resetting the
   checkout or starting any migration-capable service. Its helper uses only the existing DB
   container and PostgreSQL tools; it neither contacts Redis, lifts WRITE pause nor starts a
   quiesced writer. Keep the independent admission fence and WRITE lease uninterrupted, and
   record the predeploy path/bytes/list result from the durable Deploy log alongside the original
   maintenance archive. A failed backup prevents Switch; it is not a reason to unpause or skip
   backup on retry. The executing workflow supplies its reviewed helper even for an older target.

   ```bash
   po_compose_refresh
   po_assert_write_fence
   "${COMPOSE[@]}" stop telegram_bot langgraph engineering-worker deploy-worker architect \
     qa-worker worker-manager worker-broker scaffolder
   ```

   Keep API and infra-service running through complete Deploy. Keep all three
   schedulers running through Switch's later recreate/`--wait`; never race that
   wait with a scheduler stop. After successful Switch, they may stop while
   Reconcile finishes, or remain fenced until complete workflow success. Verify
   preflight counts after seed and after Switch using the released image; a
   paused publisher cannot rescue a failed preflight. Do not wait for PO startup.
   The WRITE fence prevents admitting new Redis work, and the independent
   external fence prevents API/Telegram/provisioning admissions across restarts.

   ```bash
   : "${PO_REDIS_DEPLOY_RUN_ID:?Set the recorded run ID}"
   gh run watch "$PO_REDIS_DEPLOY_RUN_ID" --repo vladmesh/codegen_orchestrator --exit-status
   ```

   Only after successful *complete* workflow (Switch, scheduler wait and target
   Reconcile included), repeat the whole client stop from step 2, verify no
   running/restarting client or independent process, and assert the write fence.
   Then kill deferred normal clients before unpause:

   ```bash
   po_compose_refresh
   "${COMPOSE[@]}" stop api langgraph telegram_bot scheduler-pipeline scheduler-infrastructure \
     scheduler-maintenance engineering-worker deploy-worker architect qa-worker worker-manager \
     worker-broker infra-service scaffolder
   po_assert_write_fence
   "${COMPOSE[@]}" exec -T redis redis-cli CLIENT KILL TYPE normal SKIPME yes
   "${COMPOSE[@]}" exec -T redis redis-cli CLIENT UNPAUSE
   ```

   The kill returns a count and drops stopped clients' paused commands, including
   XREADGROUP/PUBLISH, before any can commit. The converter independently proves
   drained groups. Keep external admissions fenced through final resume. For a
   failed/mismatched Deploy, leave WRITE pause active (renew before expiry), keep
   unused clients stopped, and retain API/infra-service while active dependency
   commands settle. Do not convert or unpause for availability. Follow
   [DEPLOY.md](../DEPLOY.md#if-a-deploy-fails) only within this maintenance ordering:
   an authorized retry must re-establish stable preflight, original-data readback,
   full Compose chain, unchanged key and uninterrupted write fence, then repeat
   dispatch, the limited stop, scheduler wait and complete Reconcile. Never apply
   the final full stop during a retry's dependent steps. If original drain counts
   changed, or mixed conversion/new work exists, require a recovery decision
   using the verified backups before another retry. A green earlier Switch or
   promoted record is insufficient. `up --no-deps` cannot repair a partial Deploy.
4. Verify the effective released maintenance image before conversion. In the
   dedicated operator script, refresh the array; verify checkout SHA and perform
   effective configuration/release-record, image/digest/source-label,
   no-checkout-mount/key checks from
   step 1 of the checkpoint storage steps below, using the same release SHA
   and the external backup directory. Check the stopped client list explicitly
   with `compose ps --all` and that none is running/restarting. Use the existing
   production Redis and key from that verified `langgraph` image. Build/pull no
   substitute and run no checkout-mounted maintenance code.
5. Run count-only validation, apply, then independent keyed readback, with
   producers and consumers stopped throughout. Complete checkpoint storage
   steps 2-3 below in this same quiescent interval too, before step 7:

   ```bash
   "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python langgraph \
     -m src.agents.po.redis_upgrade --writers-quiesced
   "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python langgraph \
     -m src.agents.po.redis_upgrade --writers-quiesced --apply
   "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python langgraph \
     -m src.agents.po.redis_upgrade --writers-quiesced
   ```

   Redis >= 7 is required (production pins 7.4.10). Every existing input and
   proactive group must be its released named group, have an empty PEL, and have
   consumed through `last-generated-id`; nonempty streams missing their group
   refuse. Responses/DLQs must have no groups. Unknown Redis types, unexpected
   groups, unsupported reminder/event/DLQ representations, corruption or a wrong
   key refuse with an invalid-key count before replacing any original. Valid
   released flat stream bodies, including retained poison entries, are preserved
   as evidence; the converter does not claim or migrate PEL/recovery state.
   It authenticates already protected data, encrypts plaintext, builds and reads
   back protected staging keys, WATCHes originals and staging, then atomically
   RENAMEs ready replacements. Entry IDs, stream counters/generated/deleted IDs,
   group cursors/entries-read and consumer names, reminder scores and absolute
   expiries are preserved. Empty-PEL consumer idle clocks restart; no delivery
   counter/work is lost. A rerun authenticates unchanged protected data and
   converts zero. An interrupted pre-switch leaves originals intact; a completed
   switch is protected. Temporary `po-upgrade-staging:*` remnants contain only
   ciphertext; retain them for authorized investigation until readback succeeds.

   Record `before`, `converted`/`would_convert`, `after` per input, proactive,
   response, DLQ, reminder and owner-event payload. Applied `after.*.plaintext`
   and final `would_convert` must be zero. Any refusal leaves clients stopped.
   The write fence prevents new work during Deploy. If undrained work remains,
   do not bypass the check, discard entries, or resume old code against mixed state: retain the
   backup and resolve the operation's ingress fence before retrying the switch.
6. Rewrite historical Redis persistence while writers remain stopped. Conversion
   updates current values; prior AOF commands/RDB files still contain plaintext.
   Production enables multipart AOF with `redis-server --appendonly yes`.
   Check `INFO persistence` and configuration for AOF enabled, no current save/
   rewrite and last statuses `ok`. Request BGREWRITEAOF, wait for
   `aof_rewrite_in_progress=0`, `aof_rewrite_scheduled=0` and
   `aof_last_bgrewrite_status=ok`, then BGSAVE and wait for
   `rdb_bgsave_in_progress=0`, `rdb_last_bgsave_status=ok`, with a later save time.
   Use count/status-only INFO output; inspect no payload/dump contents:

   ```bash
   "${COMPOSE[@]}" exec -T redis redis-cli BGREWRITEAOF
   "${COMPOSE[@]}" exec -T redis redis-cli INFO persistence
   # Continue after completed rewrite and the recorded successful status.
   "${COMPOSE[@]}" exec -T redis redis-cli BGSAVE
   "${COMPOSE[@]}" exec -T redis redis-cli INFO persistence
   # Continue after completed save and the recorded successful status/time.
   "${COMPOSE[@]}" stop redis
   install -d -m 0700 "$PO_REDIS_BACKUP_DIR/rewritten-data"
   "${BACKUP_DOCKER[@]}" cp "$po_redis_id:/data/." "$PO_REDIS_BACKUP_DIR/rewritten-data/"
   "${COMPOSE[@]}" up -d --no-deps --no-build --pull never redis
   ```

   Verify the rewrite by loading a *copy* of `rewritten-data` into an isolated
   Redis 7.4.10 container with no host port or production network, using the
   recorded production persistence configuration. Check Redis's AOF/RDB integrity
   tools without their repair/fix option, then run the same released keyed dry-run
   against the isolated restore and compare the six payload counts (allow only
   documented elapsed expiries). It must authenticate every restored payload and
   report zero plaintext/would-convert. Verify the AOF manifest refers only to
   the successful rewrite's current base/incremental files; account separately
   for unreferenced old files. Only resume after original Redis restart and the
   production keyed dry-run pass too. A live-value check alone does not prove a
   persistence rewrite or scrub old disk sectors. Old backups, pre-rewrite files,
   replicas, snapshots, logs and filesystem/storage history remain restricted,
   secret-bearing artifacts under their existing retention. Deletion and token
   rotation belong to separate authorized operations.
7. Resume only the released protected services. Start API and verify its health
   and config reads while external admissions stay fenced. Then start `langgraph`, confirm
   `po_consumer_started` and `reminder_poller_started` with stable startup, then
   start Telegram and the previously stopped producer/client list using
   `up -d --no-deps --no-build --pull never`. Perform full released-service
   readback after resuming, plus a harmless existing-chat follow-up and a scheduled
   reminder/notice check and prior checkpoint context/pending tool work. Verify
   new checkpoint writes with the direct storage count query below. Remove the
   independent external admission/provider freeze only after all checks pass.
   Preserve count-only evidence, no transcript. On failure stop clients again and
   retain/re-establish a verified write pause before any Deploy retry; keep the
   external fence and both backups. Recovery to old code requires the verified pre-conversion
   Redis/database backup and a separately approved recovery operation, accounting
   for forward-only database migrations and any newly admitted work.

   ```bash
   "${COMPOSE[@]}" up -d --no-deps --no-build --pull never api
   "${COMPOSE[@]}" exec -T api curl -fsS http://127.0.0.1:8000/health
   # Confirm configuration reads needed by LangGraph before continuing:
   "${COMPOSE[@]}" up -d --no-deps --no-build --pull never langgraph
   "${COMPOSE[@]}" logs --since 2m langgraph
   # Continue only after stable PO/reminder startup and both stores validate:
   "${COMPOSE[@]}" up -d --no-deps --no-build --pull never telegram_bot api \
     scheduler-pipeline scheduler-infrastructure scheduler-maintenance engineering-worker \
     deploy-worker architect qa-worker worker-manager worker-broker infra-service scaffolder
   "${COMPOSE[@]}" config --format json > "$PO_REDIS_BACKUP_DIR/compose-resumed.json"
   python3 scripts/service_release.py readback \
     --compose-config "$PO_REDIS_BACKUP_DIR/compose-resumed.json" \
     --record deployed-service-images.json --deploy-path "$PWD"
   ```

`make test-integration-po-tools` runs the real-Redis PO consumer/tool/converter
regressions alongside checkpoint tests. `make test-service SERVICE=telegram_bot`
runs real input, direct response XREAD and proactive parsing/delivery regressions.
Both CI routes use deterministic graph fixtures and harmless Telegram fixtures,
without a paid model or production action.

## Production checkpoint upgrade runbook

These are storage-specific steps within the authoritative [shared maintenance
ordering](#production-po-redis-upgrade) above. Execute its old-image drains,
stable provisioning preflight, verified Redis/database backups, uninterrupted
admission/WRITE fences, complete successful Deploy and final full client stop
first. Use its dedicated operator script, external backup directories, unchanged
key, full Compose chain and single release SHA. Do not dispatch a second Deploy,
leave queued plaintext work for a new bot, or resume either store separately.
The protected PO refuses plaintext before consuming and may restart until stopped;
green Deploy alone does not prove PO startup (`langgraph` has no healthcheck).
Every retry/recovery follows the shared sequence, retaining original data and
fences; a partially applied Switch cannot be completed with a resume command.

1. After successful complete Deploy and final full stop, verify the effective configuration
   again: both checkout files and the release override have changed. Confirm the
   release record and resolved maintenance image name the intended SHA/digest,
   contain no checkout source mounts, and have the required key. In the host shell:

   ```bash
   cd "$DEPLOY_PATH"
   po_compose_refresh
   test "$(git rev-parse HEAD)" = "$CHECKPOINT_RELEASE_SHA"
   "${COMPOSE[@]}" config --format json > "$checkpoint_backup_dir/compose-after.json"
   checkpoint_langgraph_image=$(python3 - "$checkpoint_backup_dir/compose-after.json" \
     "$CHECKPOINT_RELEASE_SHA" "$PO_REDIS_BACKUP_DIR/compose-before.json" <<'PY'
   import json
   from pathlib import Path
   import sys
   config = json.loads(Path(sys.argv[1]).read_text())
   before = json.loads(Path(sys.argv[3]).read_text())
   record = json.loads(Path("deployed-service-images.json").read_text())
   assert record["git_sha"] == sys.argv[2], "Unexpected deployed release"
   service = config["services"]["langgraph"]
   image = service["image"]
   assert image == record["images"]["langgraph"]["reference"], "Wrong maintenance image"
   assert "@sha256:" in image, "Maintenance requires a released digest"
   assert service["environment"]["SECRETS_ENCRYPTION_KEY"], "Missing checkpoint key"
   assert service["environment"]["SECRETS_ENCRYPTION_KEY"] == \
       before["services"]["langgraph"]["environment"]["SECRETS_ENCRYPTION_KEY"], "Key changed"
   policy = "PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS"
   assert config["services"]["scheduler-infrastructure"]["environment"][policy] == \
       before["services"]["scheduler-infrastructure"]["environment"][policy], "Policy changed"
   for item in config["services"].values():
       for mount in item.get("volumes", []):
           if mount["type"] != "bind":
               continue
           source = Path(mount["source"]).resolve()
           if source.is_relative_to(Path.cwd()):
               relative = source.relative_to(Path.cwd())
               assert relative.parts and relative.parts[0] in {"infra", "secrets"}, "Checkout source mount"
   print(image)
   PY
   )
   checkpoint_source_hash=$(python3 -c \
     'import json; print(json.load(open("deployed-service-images.json"))["source_hash"])')
   test "$("${BACKUP_DOCKER[@]}" image inspect --format \
     '{{index .Config.Labels "org.codegen.worker_source_hash"}}' "$checkpoint_langgraph_image")" \
     = "$checkpoint_source_hash"
   "${BACKUP_DOCKER[@]}" image inspect --format '{{.Id}}' "$checkpoint_langgraph_image"
   "${COMPOSE[@]}" ps --all api langgraph telegram_bot scheduler-pipeline scheduler-infrastructure \
     scheduler-maintenance engineering-worker deploy-worker architect qa-worker worker-manager \
     worker-broker infra-service scaffolder
   test -z "$("${COMPOSE[@]}" ps --status running --status restarting -q api langgraph telegram_bot \
     scheduler-pipeline scheduler-infrastructure scheduler-maintenance engineering-worker \
     deploy-worker architect qa-worker worker-manager worker-broker infra-service scaffolder)"
   ```

   The image must already exist locally from Deploy. `run` accepts `--pull never`
   but has no `--no-build` option; omit `--build` and fail this preflight if the
   released image is absent. `up` below explicitly uses `--no-build --pull never`.
   Do not build or pull a replacement image during maintenance. Production runtime
   mounts/configuration come from the complete Compose chain, with no `/app/src`
   or `/app/shared` checkout mounts. Full container readback is performed after
   resume; it would correctly fail for the intentionally stopped clients now.
2. With all checkpoint writers stopped, run validation/counts, conversion, then
   keyed validation again. `run --no-deps` does not start the normal PO process:

   ```bash
   "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python langgraph \
     -m src.agents.po.checkpoint_upgrade --writers-quiesced
   "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python langgraph \
     -m src.agents.po.checkpoint_upgrade --writers-quiesced --apply
   "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python langgraph \
     -m src.agents.po.checkpoint_upgrade --writers-quiesced
   ```

   Default mode is dry-run. Reports contain `before`, `would_convert` or
   `converted`, and `after`, each separated into checkpoint JSON, metadata,
   blobs and writes. Preserve these counts on the operation/sprint. Applied
   `after.*.plaintext` must all be zero; the final dry-run validates decryption
   and must report zero `would_convert`. No values, URLs, keys or row identities
   are printed. A nonzero exit means stop, retain the backup and investigate
   with writers paused. Failed conversion changes no rows. Never skip a bad row
   or delete a thread to make the command succeed.
3. Verify representation directly in PostgreSQL as well as through the command:

   ```bash
   "${COMPOSE[@]}" exec -T db sh -c \
     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
   SELECT 'checkpoint' AS payload, count(*) AS remaining_plaintext
     FROM langgraph.checkpoints WHERE NOT checkpoint ? 'po_encrypted_v1'
   UNION ALL SELECT 'metadata', count(*)
     FROM langgraph.checkpoints WHERE NOT metadata ? 'po_encrypted_v1'
   UNION ALL SELECT 'blobs', count(*) FROM langgraph.checkpoint_blobs
     WHERE type <> 'empty' AND type NOT LIKE '%+fernet'
   UNION ALL SELECT 'writes', count(*) FROM langgraph.checkpoint_writes
     WHERE type IS NULL OR type NOT LIKE '%+fernet';
   SQL
   ```

   All four counts must be zero. Envelope markers alone are not ciphertext
   validation: the final keyed dry-run above also authenticates and deserializes
   every protected payload. Record table row counts before/after if required by
   the operation; conversion updates in place and deletes no conversations.
Resume and recovery use shared step 7 only, after Redis conversion and historical
persistence verification also pass. A harmless follow-up must retain prior context
and pending tool work. Never delete a thread, corrupt reminder or notice obligation
to pass startup. Old code cannot read either converted store; a separate approved
recovery must restore the verified matching Redis/database backups before any old
writer starts, consider forward-only migrations and newly admitted work, and keep
admission fenced as described in [Rolling back](../DEPLOY.md#rolling-back).

`make test-integration-po-tools` runs the deterministic PO graph/consumer tests
against real PostgreSQL and the real API, using the service's dependency lock.
They inspect every column of all four checkpoint tables, cover user/tool/summary
canaries and resume, and capture successful and failed logs. They also test
released-row conversion, pending work, dry-run, rerun, rollback and writer locks.
