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

This is a later production operation after release. Do not start the protected PO
on an unconverted database, resume an old plaintext writer after conversion, or
run this procedure from an unmerged candidate checkout.

1. Verify the released service image and existing production
   `SECRETS_ENCRYPTION_KEY` are available. Use the same key that decrypts project
   configuration, including for the maintenance container. Do not generate a new
   key. `CHECKPOINT_DATABASE_URL` must address the intended database and select
   `langgraph` via `options=-c%20search_path%3Dlanggraph`, as in the production
   Compose file. The command refuses a different current schema. Disable shell
   tracing; keep keys and database URLs out of operation notes.
2. Pause PO ingress and drain active PO turns, then stop every `langgraph` replica
   and any independently launched PO process. The current deployment has one
   `langgraph` writer; engineering/deploy consumers use no PO saver. Pause the
   Telegram transport too, so users see the maintenance window. Scheduler and
   worker events may remain queued. Record only consumer/in-flight counts, not
   message bodies. Verify no checkpoint writer remains before acknowledging
   `--writers-quiesced`; exclusive locks are additional protection, not a substitute
   for stopping old processes.

   ```bash
   docker compose stop telegram_bot
   # Wait for already-admitted PO turns to finish, then:
   docker compose stop langgraph
   docker compose ps langgraph telegram_bot
   ```

3. Take and verify a restorable database backup while writers are stopped, before
   applying conversion. The backup and the deployed key must be available to
   the authorized recovery operator. For example, in the production project
   directory with the released Compose configuration:

   ```bash
   umask 077
   checkpoint_backup_dir=backups/po-checkpoint-upgrade
   install -d -m 0700 "$checkpoint_backup_dir"
   docker compose exec -T db sh -c \
     'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' \
     > "$checkpoint_backup_dir/before-upgrade.dump"
   test -s "$checkpoint_backup_dir/before-upgrade.dump"
   docker compose exec -T db pg_restore --list \
     < "$checkpoint_backup_dir/before-upgrade.dump" > /dev/null
   ```

   Confirm restorability through the installation's isolated restore procedure,
   not by restoring over production. Preserve the pre-RAG-drop backup from the
   issue and any other prior dumps as restricted secret-bearing artifacts (0600,
   restricted directory/access and approved retention). Neither this conversion
   nor a later encrypted dump scrubs old backups, WAL archives, replicas or copies.
   Do not delete them or claim their contents were scrubbed.
4. With the released image selected and writers still stopped, run validation and
   counts, then conversion. `run --no-deps` does not start the normal PO process:

   ```bash
   docker compose run --rm --no-deps --entrypoint python langgraph \
     -m src.agents.po.checkpoint_upgrade --writers-quiesced
   docker compose run --rm --no-deps --entrypoint python langgraph \
     -m src.agents.po.checkpoint_upgrade --writers-quiesced --apply
   docker compose run --rm --no-deps --entrypoint python langgraph \
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
5. Verify representation directly in PostgreSQL as well as through the command:

   ```bash
   docker compose exec -T db sh -c \
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
6. Resume only the released encrypted writer, then Telegram transport:

   ```bash
   docker compose up -d --no-deps langgraph
   docker compose logs --since 2m langgraph
   docker compose up -d --no-deps telegram_bot
   ```

   Check healthy PO startup and have an authorized user continue one existing
   conversation with a harmless follow-up. Confirm prior context and any pending
   tool work resume; record the result without transcript or credential values.
   Verify new writes are encrypted with the same direct storage count query.
   If recovery requires the old writer, stop the new writer first and use the
   verified backup under the recovery operation. Old code cannot read the new
   encrypted representation; never run old and new formats together.

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
