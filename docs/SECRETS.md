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
