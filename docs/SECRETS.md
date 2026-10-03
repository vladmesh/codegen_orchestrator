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
| Worker-manager preparation | Isolated Python removes released auth artifacts and installs the native helper | Sanitized origin and helper-only HOME config; no token supplied or written |
| Developer fetch/push | Native Git get calls the authenticated broker credential operation with `useHttpPath`; store/erase persist nothing | No token file, URL userinfo or HTTP auth header; no turn token cache |
| Worker GitHub CLI | Shipped `gh` entrypoint acquires a repository token for each command, then runs native gh with transient `GH_TOKEN` | No Docker `GITHUB_TOKEN`/`GH_TOKEN`; persistent `gh auth` commands refuse |

The broker and worker-manager independently authenticate the existing per-worker
broker credential and authorize `github.credential`. Manager derives project and
repository from its own `WorkerOwnership` and `repo_id`, resolves the live Repository
API record and checks project/repository equality before any mint. A request cannot
select another repository, worker or project. Missing, malformed, removed or mismatched
ownership fails closed; QA retains only its existing turn authority.

The platform alone uses `GitHubAppClient.get_repo_scoped_token` and caches it with
an approximately five-minute expiry margin. Git receives the token only in the
native protocol response; gh receives it only in one command's auth environment.
Neither argv nor logs carry it. Helper entrypoints use isolated Python and the
root-owned shared runtime, so product modules cannot shadow their imports.
Git's repeated array attributes are ignored; duplicate scalar repository fields
still refuse before credential acquisition.
Developer helper children inherit the existing broker identity; it was already
readable to the agent through `/proc`, so server authorization remains the boundary.
Worker-manager requires `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY_PATH` at startup;
production and stand mount the App key read-only into that service alone among
the two credential forwarding services. The broker and coding workers never receive it.

A failed origin cleanup or HOME configuration update refuses creation before agent
materials become visible. Preparation removes released local helper overrides,
HTTP auth headers, GitHub URL userinfo, private stores and gh auth files. It never
resets the workspace: branches, upstreams, unpushed commits, content and product
hooks survive. Infrastructure Git still disables hooks per command only.
Before each developer turn, including a reused worker, native `ls-remote origin`
must succeed. Auth/service refusal produces `repository_auth_unavailable` with
`pre_agent_refused` evidence, starts no runner and follows infrastructure parking.

`shared.diagnostics.redact_diagnostic` removes exact supplied credentials, URL userinfo
and encoded Authorization values before runner/recovery logs, output, recap/tail fields
or administrator notifications leave the boundary. Runner exceptions log sanitized text
without an original exception traceback. Worker Git failures and native container-creation
errors apply the same diagnostic boundary; useful failure context remains bounded.

#### Worker credential upgrade procedure

Activation requires a controlled drain/recreation of every released developer
container. Changing its helper cannot remove Docker's immutable token environment,
and the old wrapper cannot report the new pre-agent evidence. Do not keep a
stored-token fallback or reuse an old container after activation.

1. Quiesce engineering dispatch and wait for active turns to settle. Inventory each
   affected container, project/story/run/attempt ownership, workspace and branch.
2. Before any drain, preserve all unpublished refs with an independently verified
   Git bundle, record the local HEAD and remote tip, and retain the complete workspace
   for tracked/untracked changes, plus transcript and existing ownership evidence.
   Restrict artifact access because a released config can contain credentials.
3. Do not delete or reassign a workspace/lock that holds unpublished work. Such workers
   remain quiesced until an operator explicitly preserves and reconciles that ownership.
   In particular, story-e7e6a09f's preserved commit, workspace and bundle must remain
   recoverable; this card performs no production recovery or ownership migration.
4. After preservation and operator reconciliation, recreate workers from the new
   released image and services. The normal preparation path sanitizes released auth
   state in place while retaining local commits/content. Verify clean origin/config,
   Docker env without GitHub tokens, absent credential/gh token files, repository-bound
   helper auth and exact remote SHA readback before resuming dispatch.

This change performs no production writes. Dormant workspaces need their next normal
preparation or a later controlled upgrade. Remote deployment directories and historical
logs/argv remain separate operational obligations; platform SSH-key duplication and
post-agent publish recovery are outside this change.

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
