# Secrets Management Architecture

The secrets management architecture in Codegen Orchestrator separates the responsibilities between the Orchestrator and the user projects.

## 1. Classification of Secrets

We distinguish two levels of secrets, which have different lifecycle and storage models.

| Level | Description | Examples | Owner | Storage (Master) | Usage |
|---------|----------|---------|----------|-------------------|---------------|
| **L1. Platform** | The infrastructure keys of the Orchestrator itself | `GH_APP_PRIVATE_KEY`, `DATABASE_URL`, `OPENAI_API_KEY`, `CLOUDFLARE_API_TOKEN` | The Orchestrator's DevOps | runtime environment / deployment secret store | Injected into the service containers (Worker, API, Infra) |
| **L2. Project** | Secrets for running generated applications | `TELEGRAM_BOT_TOKEN`, `STRIPE_KEY` | The user | Encrypted project configuration and GitHub repository secrets | Deployment workflows |

---

## 2. Detailed Strategy

### Level 1: Platform Secrets (The Orchestrator)
These secrets are required for the platform itself to function.
*   **Storage**: environment variables.
*   **Access**: loaded through the service settings/configuration boundary (Pydantic settings where applicable), not by ad-hoc `os.getenv` calls in business logic.
*   **Repo**: local development may use `.env`; deployed values come from the runtime/deployment secret store and are never committed.

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
    3.  The API owns encryption at the project-config boundary. Deployment code carries secret handles through agent state and resolves plaintext only in Python at the execution boundary; plaintext is not persisted in agent state or checkpoints.
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

See [PO Redis and checkpoint production runbooks](runbooks/po-redis-and-checkpoints.md#production-po-redis-upgrade).


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

See [PO Redis and checkpoint production runbooks](runbooks/po-redis-and-checkpoints.md#production-checkpoint-upgrade-runbook).


### Infra Service (Provisioning Only)

The `infra-service` is responsible for preparing the "bare metal". It uses **L1 Secrets** only.
*   **SSH Key**: Uses Orchestrator's L1 Private Key to connect to servers.
*   **Provider Keys**: API keys for Time4VPS/DigitalOcean (L1).

It does **NOT** handle Project (L2) secrets. It does not deploy applications.

### GitHub credentials at execution boundaries

Infrastructure recovery retains the existing `deploy_project.yml` redeployment path.
It uses a repository-scoped GitHub installation token, not project application secrets.
`redeploy_service` goes through `AnsibleRunner`, the same execution boundary as provisioning.

| Boundary | Transport and lifetime | Persistent state |
|----------|------------------------|------------------|
| Scaffolder fresh/ensure workspace Git | Native `GIT_CONFIG_COUNT/KEY/VALUE` environment supplies the HTTP Authorization header for each Git subprocess | Clean GitHub origin; no stored extraheader or token |
| AnsibleRunner provisioning/recovery | JSON vars file, inventory and optional SSH key in a private `/tmp/codegen-ansible-*` directory; files created mode 0600, directory mode 0700 | `--extra-vars @<path>` contains no credential; the directory is removed on success, nonzero exit, timeout and partial setup/exception |
| Recovery target clone/update | Native Ansible copy transfers a credential store into a unique remote `/tmp/codegen-deploy-git-*` directory, mode 0700; credential file mode 0600, secret tasks use `no_log` | Clean origin; Git environment/command text names only the helper path and scope. The tagged Git block's `always` removes the directory on success, Git failure or setup failure |
| Worker-manager preparation | Docker's native exec environment carries the current token to an isolated Python writer (`python3 -I`) in the new container | Released credentialed origin replaced in place; native Git credential store at `/home/worker/.config/codegen/git-credentials`, mode 0600, parent mode 0700 |
| Developer fetch/push | Native `$HOME/.gitconfig` contains only GitHub helper/path/scope settings; HOME survives the wrapper's agent environment filter. `credential.useHttpPath` matches the repository path | Unrelated global settings survive; global config and store are atomically replaced, mode 0600. No workspace helper, token, encoded header or auth file; the private store lasts until container removal |
| Worker GitHub CLI | Creation supplies the same repository-scoped token as `GITHUB_TOKEN` and `GH_TOKEN` in Docker environment | No CLI login file is created by preparation |

Worker containers are fresh even when the scaffolded workspace is reused. Preparation
refreshes the private store for that container before checkout or instruction injection;
Git authentication therefore also works for later developer operations. Refresh replaces
the store atomically, so a later Git credential read uses the new token. Manager and
filtered agent Git use the same HOME configuration; no `GIT_*` allowlist extension is
needed. Isolated Python prevents checkout modules from shadowing the writer's stdlib.
The live refresh caller runs during container creation; each replacement container also
gets the current GitHub CLI environment. This does not introduce token rotation or a
background refresh.

Repository credentials are required for developer workspace preparation. A failed origin
upgrade, credential write or HOME configuration write refuses preparation; the worker
stays unready, gets a visible creation failure and follows the existing teardown path.
Preparation never resets the workspace: repository identity, branches, upstreams, unpushed commits and product hook
configuration survive. Infrastructure Git still disables hooks per command only.

`shared.diagnostics.redact_diagnostic` removes exact supplied credentials, URL userinfo
and encoded Authorization values before runner/recovery logs, output, recap/tail fields
or administrator notifications leave the boundary. Runner exceptions log sanitized text
without an original exception traceback. Worker Git failures and native container-creation
errors apply the same diagnostic boundary; useful failure context remains bounded.

#### Existing-workspace upgrade and production readback

The released worker-manager persisted
`https://x-access-token:<token>@github.com/<owner>/<repo>` in workspace `.git/config`.
The next worker preparation replaces this origin with the clean repository URL before
checkout and agent materials, including when no branch checkout was requested. The
existing workspace file reader can then read the real config safely; endpoint filtering
is not the upgrade mechanism. Scaffolder fresh/ensure routes continue using clean origins.

This change runs no production cleanup. After controlled release, read back prepared
workspace configs and remote deployment configs without exposing credential values.
Dormant workspaces not prepared again and remote `/opt/apps/<project>` directories not
updated again can still carry the released URLs and need a later controlled upgrade and
readback. Historical logs, argv captures and administrator messages also remain a separate
operational obligation. The production storage safeguards remain prerequisites; credential
rotation and the source issue's platform SSH-key duplication are outside this change.

### Deployment via GitHub Actions

Application deployment is fully delegated to GitHub Actions. This allows secure usage of L2 secrets without exposing them to the Orchestrator's backend.

1.  **DOTENV trick**: Orchestrator collects ALL env vars → builds `.env` content → base64-encodes → stores as single GitHub Secret `DOTENV`. The deploy workflow decodes and writes the file. No per-variable enumeration needed. Every `.env` line, the last included, ends in `\n`, so a deploy workflow may append `KEY=value` lines without corrupting the last variable.
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
