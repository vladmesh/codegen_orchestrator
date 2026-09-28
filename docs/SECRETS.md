# Secrets Management Architecture

The secrets management architecture in Codegen Orchestrator separates the responsibilities between the Orchestrator and the user projects.

## 1. Classification of Secrets

We distinguish two levels of secrets, which have different lifecycle and storage models.

| Level | Description | Examples | Owner | Storage (Master) | Usage |
|---------|----------|---------|----------|-------------------|---------------|
| **L1. Platform** | The infrastructure keys of the Orchestrator itself | `GH_APP_PRIVATE_KEY`, `POSTGRES_URL`, `OPENAI_API_KEY`, `CLOUDFLARE_API_TOKEN` | The Orchestrator's DevOps | K8s Secrets / `.env` | Injected into the service containers (Worker, API, Infra) |
| **L2. Project** | Secrets for running generated applications | `TELEGRAM_BOT_TOKEN`, `STRIPE_KEY` | The user | Encrypted project configuration and GitHub repository secrets | Deployment workflows |

---

## 2. Detailed Strategy

### Level 1: Platform Secrets (The Orchestrator)
These secrets are required for the platform itself to function.
*   **Storage**: environment variables.
*   **Access**: read at service startup (`os.getenv`).
*   **Repo**: stored in `.env` (locally) or in the Secret Manager of the hosting platform.

`WORKER_BROKER_INTERNAL_TOKEN` is an L1 credential shared only by
`worker-manager` and `worker-broker`. It authenticates worker registration and
must be non-empty before either service starts. Coding workers receive only a
distinct per-worker broker credential.

### Level 2: Project Secrets (The Generated App)
The key point: **the secrets are encrypted at rest in PostgreSQL** (Fernet encryption).
*   **Storage**: `project.config.secrets` (JSONB) — all values are encrypted as Fernet tokens (`gAAAAA...`).
*   **Encryption**: `shared/crypto.py` — `SecretsCipher` reads `SECRETS_ENCRYPTION_KEY` from the env. `encrypt_dict`/`decrypt_dict` encrypt/decrypt all the values in a dict.
*   **Lifecycle**:
    1.  The user enters a token (for example, a Telegram Token) through the PO in Telegram.
    2.  PO tool `set_project_secret` → atomic merge via `POST /projects/{id}/config/secrets` (server-side `SELECT FOR UPDATE` locking, handles concurrent writes).
    3.  DevOps subgraph `SecretResolverNode` → decrypt from DB → resolve → encrypt → save back via atomic merge.
*   **Usage**:
    *   The secrets are available decrypted only at runtime (when `decrypt_dict` is called)
    *   In the DB they are always encrypted — even a direct SELECT shows only Fernet tokens

## 3. Integration with Components

### PO Redis dialogue and payload caches

The invariant is ordered: protect every user-controlled or model-echoed payload
before Redis first receives it; authenticate/decode inside trusted consumers
before validation and delivery; protect quarantine and keep failure diagnostics
free of payloads. Opaque passwords, names, tool arguments and arbitrary summaries
make pattern recognition insufficient. The whole payload uses the existing
`shared.crypto.SecretsCipher` and deployed `SECRETS_ENCRYPTION_KEY`, with no new
key, dependency, or runtime plaintext fallback. The Fernet envelope includes the
Redis destination, so moving a token to a different stream/cache fails authentication.

| Storage | Released writers | Trusted readers | Protected content |
|---|---|---|---|
| `po:input` | Telegram `main._send_to_po_and_wait`; scheduler `owner_notifications` and `supervisor.stage_notices`; LangGraph engineering/deploy/Architect callback and story helpers; reminder poller | PO `consume_typed` | All logical flat fields, including text, user name, QA facts and notice reference |
| `po:response:<request>` | PO consumer success/fixed-error replies | Telegram `main._read_po_response`, direct XREAD | All response fields |
| `po:proactive` | PO consumer gated final reply; `notify_user`; `_events.publish_proactive_message` | Telegram `consume` then `process_proactive_entry` | All fields, including model echoes and delivery identifiers |
| PO stream `:dlq` | Shared Redis quarantine | Authorized maintenance/debugging consumers | Entire quarantine record, including original wire body and reason |
| `po:reminders` | PO `set_reminder`, ZADD | PO reminder poller | Entire JSON member, including text/reason and addressing |
| `po:latest_owner_event:<chat>:<story>` | PO `remember_owner_event`, SET | `suppress_owner_notice` | Entire `POSystemEvent`, including text, QA facts and durable notice reference |

Only Redis key names (including request/chat/story identifiers), stream IDs,
group/consumer names, cursors/PEL delivery counts, sorted-set fire scores and
TTLs remain clear. The envelope marker and Fernet's version/time framing also
remain visible. No payload field needs cleartext routing. `po:last_user_message`
holds only a timestamp; story-gate/stage-notice keys hold IDs, status and timing,
not dialogue. The stand's direct PO Redis probes resolve the key inside the
released LangGraph container through `shared.redis.po_cli`; they pass logical
data only to the trusted test process and do not export the key.

The logical DTOs, recipient resolution, admission, locks and at-least-once
delivery stay unchanged. Quarantine must succeed before ACK; failed encryption
or missing/invalid configuration refuses before XADD/SET/ZADD. Startup readback
refuses plaintext/corruption without ACKing or moving it. Runtime poison entries
arriving after startup produce encrypted evidence; failed quarantine stays
pending for the existing reclaim path. A corrupt reminder remains stored for
repair. Transport errors use fixed text or exception classes, never the rejected
payload or a decrypted exception body. Notice deferral reasons remain available
to notice logic but are omitted from transport logs.

#### Production PO Redis upgrade

This is a separate authorized production operation after merge/release, not an
action performed by this code card. Reserve an exclusive maintenance window
against Deploy, schedulers, independent processes and manual restarts. Use the
existing key that decrypts project secrets/checkpoints. Old code cannot read
converted PO payloads; never resume an old producer/consumer after conversion.
If checkpoint conversion is still needed, complete its stopped-writer runbook
below in the same window before resuming PO.

Execute every strict shell block below in a dedicated Bash script or subshell,
not the interactive control pane. Keep configs, dumps, old drain logs and copies
of Redis `/data` outside the checkout, in an operator-owned 0700 directory with
0600 files. Never attach them to reports. Set `PO_REDIS_BACKUP_DIR` to an absolute,
external path, `PO_REDIS_RELEASE_SHA` to the full released main SHA, and retain
the deployment environment's complete `DEPLOY_PATH` and `COMPOSE_ARGS`. The
default Compose chain is `-f docker-compose.yml -f docker-compose.prod.yml`;
h01o also has its configured absolute host override. Append the deployment's
digest override last, in the same project/directory as Deploy:

```bash
(
  set +x
  set -euo pipefail
  umask 077
  : "${DEPLOY_PATH:?}"
  : "${COMPOSE_ARGS:?}"
  : "${PO_REDIS_RELEASE_SHA:?}"
  : "${PO_REDIS_BACKUP_DIR:?}"
  test "${PO_REDIS_BACKUP_DIR:0:1}" = /
  cd "$DEPLOY_PATH"
  case "$PO_REDIS_BACKUP_DIR/" in "$PWD/"*) exit 1 ;; esac
  install -d -m 0700 "$PO_REDIS_BACKUP_DIR"
  read -r -a po_compose_args <<< "$COMPOSE_ARGS"
  COMPOSE=(docker compose "${po_compose_args[@]}" -f deployed-service-images.compose.yml)
  "${COMPOSE[@]}" config --format json > "$PO_REDIS_BACKUP_DIR/compose-before.json"
  python3 scripts/service_release.py readback \
    --compose-config "$PO_REDIS_BACKUP_DIR/compose-before.json" \
    --record deployed-service-images.json --deploy-path "$PWD"
)
```

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
   docker kill --signal SIGTERM "$po_old_bot_id" > /dev/null
   test "$(docker wait "$po_old_bot_id")" = 0
   ```

   PO can still publish proactive replies after the bot exits. Drain them with
   a one-off process from that same old `telegram_bot` digest, using the released
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

   Confirm input/proactive are drained, let active PO turns finish, then stop
   `langgraph`. If a due reminder raced this stop, the converter's unread/PEL
   precondition refuses; finish the old drain before switching images. Future
   reminders and durable notice obligations are retained, never fired early or
   expired to pass the check. Retained response streams are preserved too;
   finish every old bot waiter before replacing its image. A blocked audience,
   notice lookup or pending handler must be resolved with the old supported
   consumer, not a new recovery subsystem or forced acknowledgement.
2. With all Redis clients quiesced, take a consistent, verified backup. Stop
   Redis, copy the complete `/data` directory (Redis 7 multipart AOF directory,
   manifest, any RDB, not just a guessed `appendonly.aof` filename), and verify
   isolated restore with the released old image/key before proceeding. Keep the
   corresponding database/checkpoint backup if needed. Capture only counts and
   backup digests in operation notes:

   ```bash
   "${COMPOSE[@]}" stop api langgraph telegram_bot scheduler-pipeline scheduler-infrastructure \
     scheduler-maintenance engineering-worker deploy-worker architect qa-worker worker-manager \
     worker-broker infra-service scaffolder
   "${COMPOSE[@]}" stop redis
   po_redis_id=$("${COMPOSE[@]}" ps --all -q redis)
   test -n "$po_redis_id"
   install -d -m 0700 "$PO_REDIS_BACKUP_DIR/before-data"
   docker cp "$po_redis_id:/data/." "$PO_REDIS_BACKUP_DIR/before-data/"
   "${COMPOSE[@]}" up -d --no-deps --no-build --pull never redis
   ```

   Account for every independent worker/Redis client before asserting quiescence.
   Backups contain historical plaintext even if the current dataset looks clean.
   Do not restore over production or delete restricted old artifacts here.
3. Dispatch the actual released Deploy from the authorized control shell:

   Fence external admissions independently of containers (the workflow restarts
   the API/proxy too), and pause Redis writes across the restart gap. In the host
   operator script record the Redis container ID/start time and the UTC deadline
   of a two-hour `CLIENT PAUSE WRITE` lease, then:

   ```bash
   po_redis_id=$("${COMPOSE[@]}" ps -q redis)
   po_redis_started=$(docker inspect --format '{{.State.StartedAt}}' "$po_redis_id")
   "${COMPOSE[@]}" exec -T redis redis-cli CLIENT PAUSE 7200000 WRITE
   ```

   Administrative/count reads remain available; no restarted PO producer can
   XADD/SET/ZADD before the post-Deploy stop. Redis must not restart/recreate and
   the lease must not expire during this step. Record and verify those facts,
   never assume them from workflow success. If either changes, keep services
   stopped and abort this operation for an approved drain/restore decision.

   ```bash
   gh workflow run deploy.yml --repo vladmesh/codegen_orchestrator --ref main \
     -f environment=production -f revision="$PO_REDIS_RELEASE_SHA"
   ```

   Record its run ID. Deploy rewrites `.env`, switches checkout/release records
   and calls all-service `up -d --remove-orphans --no-build --pull never`, so the
   earlier stops do not survive it. As soon as Switch runs that `up`, reconstruct
   the full current Compose array and stop all clients listed in step 2 again.
   Do not wait for a green workflow or PO startup. Startup refuses old payloads;
   the verified Redis write fence prevents restarted schedulers/workers from
   admitting protected work during this gap.
   Deploy later recreates the schedulers: repeat their stop after the workflow
   finishes. After every client is stopped, verify the same Redis ID/start time
   and unexpired pause lease, then run `redis-cli CLIENT KILL TYPE normal SKIPME yes`
   through the Redis container (count-only result) before `CLIENT UNPAUSE`. This
   drops the stopped clients' paused commands before writes resume. Deferred
   writes are not committed; the converter independently proves drained groups.
   Keep the external admissions fence through final resume. Every Deploy retry
   requires the write fence and both stops again. A failed/mismatched
   Deploy stays stopped and follows [DEPLOY.md](DEPLOY.md#if-a-deploy-fails).
4. Verify the effective released maintenance image before conversion. In the
   dedicated operator script, refresh the array; verify checkout SHA, run
   `scripts/service_release.py readback` on restricted effective Compose JSON,
   and perform the image/digest/source-label/no-checkout-mount/key checks from
   step 6 of the checkpoint runbook below, substituting `PO_REDIS_RELEASE_SHA`
   and the external backup directory. Check the stopped client list explicitly
   with `compose ps --all` and that none is running/restarting. Use the existing
   production Redis and key from that verified `langgraph` image. Build/pull no
   substitute and run no checkout-mounted maintenance code.
5. Run count-only validation, apply, then independent keyed readback, with
   producers and consumers stopped throughout:

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
   docker cp "$po_redis_id:/data/." "$PO_REDIS_BACKUP_DIR/rewritten-data/"
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
7. Resume only the released protected services. Start `langgraph`, confirm
   `po_consumer_started` and `reminder_poller_started` with stable startup, then
   start Telegram and the previously stopped producer/client list using
   `up -d --no-deps --no-build --pull never`. Perform full released-service
   readback after resuming, plus a harmless existing-chat follow-up and a scheduled
   reminder/notice check. Preserve count-only evidence, no transcript. On failure
   stop clients again. Recovery to old code requires the verified pre-conversion
   Redis/database backup and a separately approved recovery operation, accounting
   for forward-only database migrations and any newly admitted work.

`make test-integration-po-tools` runs the real-Redis PO consumer/tool/converter
regressions alongside checkpoint tests. `make test-service SERVICE=telegram_bot`
runs real input, direct response XREAD and proactive parsing/delivery regressions.
Both CI routes use deterministic graph fixtures and harmless Telegram fixtures,
without a paid model or production action.

### PO conversation checkpoints

`agents/po/checkpoints.py::ProtectedPostgresSaver` is the single persistence boundary
for the PO graph. It uses LangGraph's `EncryptedSerializer` with an adapter to the
existing `SecretsCipher` and `SECRETS_ENCRYPTION_KEY`. The bytes-to-text base64
transport is inside authenticated Fernet encryption. No secret recognition rules,
new key or runtime dependency are involved. Project-secret endpoints and PO tools
retain their existing behavior; this change protects conversation copies at rest.

The inventory for the released `langgraph-checkpoint-postgres==3.0.4` is:

| Table / field | Stored content | Protection |
|---|---|---|
| `langgraph.checkpoints.checkpoint` | Inline primitive channel values, checkpoint state and bookkeeping | `po_encrypted_v1` authenticated envelope; only `v` and `channel_versions` remain visible for the saver's SQL joins and pending-send migration |
| `langgraph.checkpoints.metadata` | Source, step, parents, run metadata and copied configurable values (including user name) | Entire JSONB document in an authenticated envelope |
| `langgraph.checkpoint_blobs.blob` | Messages, user text, AI tool-call arguments, tool results, `context.running_summary`, `llm_input_messages` and other non-primitive channels | Native typed serialization followed by Fernet; type is suffixed `+fernet` |
| `langgraph.checkpoint_writes.blob` | Pending channel updates, tool work/results, input, interrupts, errors and pre-v4 pending sends | Same encrypted serializer, including null writes |
| `langgraph.checkpoint_migrations.v` | Saver migration numbers | No dialogue payload |

Other columns contain graph-controlled identifiers: thread/chat ID, namespace,
checkpoint and parent IDs, channel name/version, serializer type, task ID/path and
write index. They are neither message content nor secret values. The unused
`checkpoints.type` column remains unchanged. RAG dialogue tables were removed by
`d3f5a7c9e1b4`; this boundary does not recreate them.

Native encrypted serialization alone is insufficient: the saver bypasses it for
inline primitives and metadata. The PO extension changes only that JSONB write
and its corresponding read, retaining native blob/write SQL, versioning, parent
links and pending-write ordering. All serialization and encryption for a checkpoint
finish before its first INSERT. Native pending writes likewise serialize the whole
batch before their INSERT. Normal consumer invocations, its one corruption retry,
orphan repair (`aupdate_state`) and the real summarization
hook all use this same saver. Metadata filters decrypt before filtering and preserve
the native containment behavior, with no plaintext filter values sent to PostgreSQL.

A missing/invalid key fails before setup writes. Plaintext rows refuse PO startup;
invalid ciphertext and wrong keys fail on read with payload-free errors. The PO
consumer logs exception classes instead of model/tool exception text or traces,
and corruption retry logs no raw error. Successful logs contain counts and IDs.
Dialogue is decrypted in process for the supported conversation and tool behavior;
this is an at-rest boundary, not a change to model input or queue transport.

Runtime reads accept only encrypted payloads. Released native JSON checkpoint
versions 1–4 and native `json`, `msgpack`, `bytes`, `bytearray` and `null` blobs are
accepted only by the explicit maintenance conversion below. The upstream pre-v4
pending-send reader still runs after conversion, against encrypted write blobs.
There is no permanent plaintext runtime fallback. Conversion requires quiesced
writers, holds exclusive locks on all three payload tables, validates existing
ciphertext with the configured key, and commits all rows in one transaction.
Failure rolls back every thread/table. Reruns validate encrypted payloads and
convert zero rows. Large installations need space for the encrypted data and WAL
and a maintenance window for the table scan and transaction; do not slice the
transaction while keeping writers live.

### Production checkpoint upgrade runbook

This is a later production operation on a released main revision, never on an
unmerged candidate. The supported [Deploy workflow](DEPLOY.md#updating) starts
all services, including `langgraph` and `telegram_bot`; it has no maintenance-pause
input. The protected PO refuses plaintext rows before consuming messages and may
restart until explicitly stopped. A green Deploy does not prove healthy PO startup
in this window: `langgraph` has no healthcheck. Arrange an operator to stop the
restarted services as soon as the workflow's Switch has run `up`.

1. Reserve the maintenance window against concurrent deploys/manual restarts.
   Obtain the released full main SHA and the deployment environment's exact
   `DEPLOY_PATH` and `COMPOSE_ARGS`. The default chain is
   `-f docker-compose.yml -f docker-compose.prod.yml`; on h01o, retain its additional
   absolute-path host override from `COMPOSE_ARGS`, in the same order. Do not
   substitute the default for a host's configured chain. Append the live
   `deployed-service-images.compose.yml` last, as Deploy does. All host commands
   below use this Bash array, the same deploy directory/project and its `.env`:

   ```bash
   set +x
   set -euo pipefail
   umask 077
   : "${DEPLOY_PATH:?Set the deployment environment's DEPLOY_PATH}"
   : "${COMPOSE_ARGS:?Set the deployment environment's complete COMPOSE_ARGS}"
   : "${CHECKPOINT_RELEASE_SHA:?Set the full released main SHA}"
   cd "$DEPLOY_PATH"
   checkpoint_compose_refresh() {
     read -r -a checkpoint_compose_args <<< "$COMPOSE_ARGS"
     COMPOSE=(docker compose "${checkpoint_compose_args[@]}" \
       -f deployed-service-images.compose.yml)
   }
   checkpoint_compose_refresh
   checkpoint_backup_dir="$PWD/backups/po-checkpoint-upgrade-$(date -u +%Y%m%dT%H%M%SZ)"
   install -d -m 0700 "$checkpoint_backup_dir"
   "${COMPOSE[@]}" config --format json > "$checkpoint_backup_dir/compose-before.json"
   python3 scripts/service_release.py readback \
     --compose-config "$checkpoint_backup_dir/compose-before.json" \
     --record deployed-service-images.json --deploy-path "$PWD"
   ```

   `COMPOSE_ARGS` uses the workflow's whitespace-separated CLI arguments; its
   file paths must not contain spaces. Verify the existing production
   `SECRETS_ENCRYPTION_KEY` will remain unchanged through Deploy, including in the
   maintenance container. Use the key that decrypts project configuration; do not
   generate a new one. `CHECKPOINT_DATABASE_URL` must address the intended database
   and select `langgraph` via `options=-c%20search_path%3Dlanggraph`, as in Compose.
   The maintenance command refuses a different current schema. Rendered config
   contains credentials: keep these files restricted and never attach them or
   keys/URLs to operation notes. Record only safe digest/count/readback evidence.
2. Pre-deploy: pause PO ingress and drain active PO turns, then stop every
   `langgraph` replica and any independently launched PO process. The current deployment has one
   `langgraph` writer; engineering/deploy consumers use no PO saver. Pause the
   Telegram transport too, to prevent new admissions. Scheduler and
   worker events may remain queued. Record only consumer/in-flight counts, not
   message bodies. Verify no checkpoint writer remains before acknowledging
   `--writers-quiesced`; exclusive locks are additional protection, not a substitute
   for stopping old processes.

   Preserve Telegram pending updates and Redis streams/consumer-group pending
   entries: do not flush, trim, acknowledge or delete them to clear the window,
   and do not use Telegram's drop-pending-updates option.

   ```bash
   "${COMPOSE[@]}" stop telegram_bot
   # Wait for already-admitted PO turns to finish, then:
   "${COMPOSE[@]}" stop langgraph
   "${COMPOSE[@]}" ps --all langgraph telegram_bot
   test -z "$("${COMPOSE[@]}" ps --status running --status restarting -q langgraph telegram_bot)"
   ```

3. Take and verify a restorable database backup while writers are stopped, before
   applying conversion. The backup and the deployed key must be available to
   the authorized recovery operator. In the same host shell:

   ```bash
   "${COMPOSE[@]}" exec -T db sh -c \
     'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' \
     > "$checkpoint_backup_dir/before-upgrade.dump"
   test -s "$checkpoint_backup_dir/before-upgrade.dump"
   "${COMPOSE[@]}" exec -T db pg_restore --list \
     < "$checkpoint_backup_dir/before-upgrade.dump" > /dev/null
   ```

   Confirm restorability through the installation's isolated restore procedure,
   not by restoring over production. Preserve the pre-RAG-drop backup from the
   issue and any other prior dumps as restricted secret-bearing artifacts (0600,
   restricted directory/access and approved retention). Neither this conversion
   nor a later encrypted dump scrubs old backups, WAL archives, replicas or copies.
   Do not delete them or claim their contents were scrubbed.
4. Dispatch the actual Deploy workflow for the released revision from an
   authorized control shell with `CHECKPOINT_RELEASE_SHA` set to the same SHA:

   ```bash
   gh workflow run deploy.yml --repo vladmesh/codegen_orchestrator --ref main \
     -f environment=production -f revision="$CHECKPOINT_RELEASE_SHA"
   ```

   Record this dispatch's run ID. Deploy pulls and verifies the release, rewrites
   the host `.env`, switches the checkout and image override, and runs
   `up -d --remove-orphans --no-build --pull never` for all services. Thus the old
   writer stop above does not survive Deploy. Any newly admitted PO updates wait
   in Redis while the protected writer refuses the unconverted rows. No old
   writer may run concurrently with this switch or the conversion.
5. Post-deploy stop: as soon as Switch has run its all-service `up`, return to the
   host shell and reconstruct the chain from the current deployment environment
   values. Do not wait for green before stopping the restarted ingress and PO:

   ```bash
   cd "$DEPLOY_PATH"
   checkpoint_compose_refresh
   "${COMPOSE[@]}" stop telegram_bot langgraph
   "${COMPOSE[@]}" ps --all langgraph telegram_bot
   test -z "$("${COMPOSE[@]}" ps --status running --status restarting -q langgraph telegram_bot)"
   ```

   Confirm every independent PO writer is also stopped. Allow the remainder of
   Deploy to finish (its later `up` recreates only schedulers). In the control shell:

   ```bash
   : "${CHECKPOINT_DEPLOY_RUN_ID:?Set the recorded Deploy run ID}"
   gh run watch "$CHECKPOINT_DEPLOY_RUN_ID" --repo vladmesh/codegen_orchestrator --exit-status
   ```

   If Deploy fails, keep PO/ingress stopped and follow
   [deploy failure recovery](DEPLOY.md#if-a-deploy-fails); do not convert a
   partially applied target or mismatched release records. Every retry starts
   services again and requires this post-deploy stop again.
6. After successful Deploy, reconstruct and verify the effective configuration
   again: both checkout files and the release override have changed. Confirm the
   release record and resolved maintenance image name the intended SHA/digest,
   contain no checkout source mounts, and have the required key. In the host shell:

   ```bash
   cd "$DEPLOY_PATH"
   checkpoint_compose_refresh
   test "$(git rev-parse HEAD)" = "$CHECKPOINT_RELEASE_SHA"
   "${COMPOSE[@]}" config --format json > "$checkpoint_backup_dir/compose-after.json"
   checkpoint_langgraph_image=$(python3 - "$checkpoint_backup_dir/compose-after.json" \
     "$CHECKPOINT_RELEASE_SHA" <<'PY'
   import json
   from pathlib import Path
   import sys
   config = json.loads(Path(sys.argv[1]).read_text())
   record = json.loads(Path("deployed-service-images.json").read_text())
   assert record["git_sha"] == sys.argv[2], "Unexpected deployed release"
   service = config["services"]["langgraph"]
   image = service["image"]
   assert image == record["images"]["langgraph"]["reference"], "Wrong maintenance image"
   assert "@sha256:" in image, "Maintenance requires a released digest"
   assert service["environment"]["SECRETS_ENCRYPTION_KEY"], "Missing checkpoint key"
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
   test "$(docker image inspect --format \
     '{{index .Config.Labels "org.codegen.worker_source_hash"}}' "$checkpoint_langgraph_image")" \
     = "$checkpoint_source_hash"
   docker image inspect --format '{{.Id}}' "$checkpoint_langgraph_image"
   "${COMPOSE[@]}" ps --all langgraph telegram_bot
   test -z "$("${COMPOSE[@]}" ps --status running --status restarting -q langgraph telegram_bot)"
   ```

   The image must already exist locally from Deploy. `run` accepts `--pull never`
   but has no `--no-build` option; omit `--build` and fail this preflight if the
   released image is absent. `up` below explicitly uses `--no-build --pull never`.
   Do not build or pull a replacement image during maintenance. Production runtime
   mounts/configuration come from the complete Compose chain, with no `/app/src`
   or `/app/shared` checkout mounts. Full container readback is performed after
   resume; it would correctly fail for the two intentionally stopped services now.
7. With all checkpoint writers stopped, run validation/counts, conversion, then
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
8. Verify representation directly in PostgreSQL as well as through the command:

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
9. Resume only the released encrypted writer. Confirm `po_consumer_started` and
   stable startup (not merely `po_consumer_enabled` or a running/restarting
   container) before resuming Telegram transport:

   ```bash
   "${COMPOSE[@]}" up -d --no-deps --no-build --pull never langgraph
   "${COMPOSE[@]}" logs --since 2m langgraph
   # Continue only after healthy PO startup is confirmed:
   "${COMPOSE[@]}" up -d --no-deps --no-build --pull never telegram_bot
   "${COMPOSE[@]}" config --format json > "$checkpoint_backup_dir/compose-resumed.json"
   python3 scripts/service_release.py readback \
     --compose-config "$checkpoint_backup_dir/compose-resumed.json" \
     --record deployed-service-images.json --deploy-path "$PWD"
   ```

   Check healthy PO startup and have an authorized user continue one existing
   conversation with a harmless follow-up. Confirm prior context and any pending
   tool work resume; record the result without transcript or credential values.
   Verify new writes are encrypted with the same direct storage count query.
   If startup, readback or the conversation check fails, stop both services using
   `"${COMPOSE[@]}" stop telegram_bot langgraph` and retain the backup, key and
   count evidence for recovery. Old code cannot read converted rows: never restart
   an old writer against them or run old/new writers together. Recovery to an old
   writer requires a separate approved recovery operation restoring the verified
   pre-conversion backup before that writer starts, with forward-only migrations
   considered as described in [Rolling back](DEPLOY.md#rolling-back).

`make test-integration-po-tools` runs the deterministic PO graph/consumer tests
against real PostgreSQL and the real API, using the service's dependency lock.
They inspect every column of all four checkpoint tables, cover user/tool/summary
canaries and resume, and capture successful and failed logs. They also test
released-row conversion, pending work, dry-run, rerun, rollback and writer locks.

### Infra Service (Provisioning Only)

The `infra-service` is responsible for preparing the "bare metal". It uses **L1 Secrets** only.
*   **SSH Key**: Uses Orchestrator's L1 Private Key to connect to servers.
*   **Provider Keys**: API keys for Time4VPS/DigitalOcean (L1).

It does **NOT** handle Project (L2) secrets. It does not deploy applications.

### Deployment via GitHub Actions

Application deployment is fully delegated to GitHub Actions. This allows secure usage of L2 secrets without exposing them to the Orchestrator's backend.

1.  **DOTENV trick**: Orchestrator collects ALL env vars → builds `.env` content → base64-encodes → stores as single GitHub Secret `DOTENV`. The deploy workflow decodes and writes the file. No per-variable enumeration needed.
2.  **Secret Injection** (three stages):
    *   **Scaffolder**: Sets `REGISTRY_URL`, `REGISTRY_USER`, `REGISTRY_PASSWORD` immediately after repo creation (before first CI push)
    *   **scheduler-pipeline**: The PR poller rewrites the same three immediately before every merge, and on every tick a PR is still armed for GitHub auto-merge from an earlier release; it is the only automated merger (no GitHub auto-merge), so push-main CI never starts on stale or absent registry secrets; a failed write blocks the merge
    *   **DeployerNode**: Sets 9 secrets total — `DOTENV`, `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PORT`, `PROJECT_NAME`, `REGISTRY_URL`, `REGISTRY_USER`, `REGISTRY_PASSWORD`
3.  **CI workflow** (`ci.yml`, on push): lint → test → build images → push to self-hosted Docker registry
4.  **Deploy workflow** (`deploy.yml`, on `workflow_dispatch` from Orchestrator): SCP compose files → write `.env` from DOTENV → pull images → `docker compose up`

**Privilege Separation:**
*   **Infra Service**: Can create/destroy servers (Root access via Ansible). Cannot see App Secrets.
*   **GitHub Actions**: Can deploy apps (SSH User access). Can access App Secrets. Cannot destroy servers.

**Docker Registry**: Self-hosted (`registry:2`) behind Caddy with TLS and basic auth. CI pushes images there, deploy pulls from there. GHCR is not used (GitHub App tokens cannot create org packages).

---

## 4. Summary of Flows

1.  **User creates Project** → Orchestrator creates GitHub Repo + sets registry secrets (`REGISTRY_*`).
2.  **User provides Bot Token** → PO tool `set_project_secret` → encrypted in DB (Fernet).
3.  **Infra Service provisions Server** → Uses L1 Keys (Time4VPS API) for server setup. Ansible playbooks for Docker/firewall/users.
4.  **Scaffolder pushes code** → CI (`ci.yml`, auto on push) → builds Docker images → pushes to self-hosted registry.
5.  **Orchestrator triggers deploy** → DevOps subgraph: environment-contract resolution → DOTENV → GitHub Secrets → `workflow_dispatch deploy.yml` → pull images from registry → `docker compose up`.
6.  **Feature deploy** → the `scheduler-pipeline` PR poller writes the registry secrets, then merges → push-main CI builds the merge commit's images → the PR poller observes them → `deploy:queue` → re-resolve env → deploy.
