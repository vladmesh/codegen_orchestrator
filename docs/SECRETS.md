# Secrets Management Architecture

Explicit commit recovery uses authenticated API-to-manager connectivity
(`WORKER_MANAGER_URL` is required) and the existing own-repo scoped mint/cache.
Each native publication reacquires the token, including margin-based expiry
refresh, after validating a bounded manager-owned object snapshot. Privileged Git
never runs in the worker checkout, even for preservation inspection or object
export. Only source object bytes and corroborated refs cross that boundary;
worker config, scripts, hooks, alternates and environment cannot run in manager
context. The clean repository has manager-owned config/home, disabled askpass/hooks,
no credential helpers/proxy/redirects, and exact own-repo HTTPS transport. Only
transient Git environment config receives its URL-scoped Basic header. Neither
API claims/receipts, Git argv, checkout files nor diagnostics contain tokens.
Recovery starts no agent and cleanup removes only its temporary snapshot.
See [the recovery runbook](runbooks/preserved-commit-recovery.md).

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

Stand model sessions follow `scripts.stand_run.requires_model_sessions`: only a
registered no-model suite after alias resolution may bypass them. Raw targets,
including `tests/live/test_llm_channel_failover.py`, and absent selections retain
full session/profile protection. `Suite.llm` continues to select the developer/QA
executor environment independently; it does not classify a raw target's other calls.
`mega-noop` requires no `CODEX_AUTH_JSON`, Claude OAuth token/expiry or QA Telethon
session. It restores and installs no profile, performs no model authentication
request and writes no rotated session. The stand's empty profile mount directory
is not an authenticated session. Required service and infrastructure secrets remain
fail-closed, including PO/Architect service API credentials and the GitHub App key.

Explicit paid suites retain refreshable Codex profile validation, exact-image
authentication before provisioning, durable preflight refresh writes and protected
remote refresh retrieval/writeback. Tokens travel only through protected files or
stdin, never argv or logs. Handoff admission scans profile token generations before
writing its value-free redaction attestation; cleanup requires that attestation for
paid suites. No-model admission scans every supplied protected value even when its
unused session is optional, and never fabricates a profile-backed attestation.

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
Codex developer shells use an explicit process-and-broker allowlist, overriding
the mounted profile's shell policy without placing credential values in argv.
QA retains its separate restricted environment and receives no developer policy.
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
2.  **Secret Injection**: every write of `REGISTRY_URL`, `REGISTRY_USER` and `REGISTRY_PASSWORD` goes through the registry-secrets receipt (4); no other runtime path sends them.
    *   **Repository setup** (`engineering-worker`, `consumers/_repo_setup.py`) and the **scaffolder** full scaffold make the three current right after creating the repository, before the first push starts CI. A refusal fails the setup or scaffold; nothing is recorded as set up.
    *   **scheduler-pipeline**: the PR poller makes the three current immediately before every merge, and on every tick a PR is still armed for GitHub auto-merge from an earlier release, before it withdraws that request. It is the only automated merger (no GitHub auto-merge), so push-main CI never starts on stale or absent registry secrets. A matching receipt suffices. Lease contention (`registry_secrets_lease_busy`, `registry_secrets_lease_lost`) leaves the PR for the next tick; any other refusal parks the story; a Redis or repository-read failure skips the merge for that tick.
    *   **DeployerNode** (`subgraphs/devops/deploy_secrets.py`): every deploy needs nine secrets set before it dispatches `deploy.yml`. On every setup attempt it makes the three registry secrets current through the receipt, with the project deploy lock as the caller lock, and it is the only writer of the other six: `DOTENV`, `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PORT`, `PROJECT_NAME`. Those six are skipped, with their public-key reads, while the deploy-secrets receipt vouches for them (3). The deploy succeeds only when both groups are known complete; `deploy_secrets_configured` logs `secrets_count` written now and `cached_count` certified by a receipt, out of `total` nine. An unchanged redeploy sends no secret PUT and reads no public key (the repository identity is still read).
3.  **Deploy-secrets receipt**: Redis key `deploy:<project_id>:github-secrets` holds one SHA-256 over the GitHub repository id (one `get_repo` read per deploy) and the six resolved names and values in canonical order; never a value, key or per-value digest. A deploy whose digest matches writes none of the six. Any other receipt (absent, corrupt, another digest, a recreated repository) is a miss: the deploy deletes the receipt before writing, writes all six, and sets the new digest only after the six and the registry three are known complete. Reading, deleting and setting it are Lua scripts that first compare the project deploy lock with this deploy's token, and every secret PUT attempt checks the lock, so a deploy that lost the lock fails `deploy_lock_lost` and certifies nothing. A partial or interrupted write leaves no receipt, so the next deploy writes all six again; a Redis or repository-read failure refuses the deploy rather than counting as a hit. Losing the receipt costs one write of the six. The digest attests what the orchestrator last wrote, not what GitHub holds: after a deploy secret is edited or deleted outside the orchestrator, delete the receipt before the next deploy with `docker compose exec -T redis redis-cli DEL deploy:<project_id>:github-secrets`. As with every deploy write, a PUT already in flight when the lock expires is not revoked.
4.  **Registry-secrets receipt**: `GitHubAppClient.ensure_registry_secrets(owner, repo, redis=..., lease_seconds=..., guard=..., caller_lock=...)` makes the three `REGISTRY_*` current under one lease per repository, and is the only runtime path that writes them. Every writer passes its own Redis connection and `GITHUB_REGISTRY_SECRETS_LEASE_SECONDS`, a required positive integer with no default (180 on production and the stand); scheduler-pipeline, engineering-worker, scaffolder and deploy-worker refuse to start without it or without a usable `SECRETS_ENCRYPTION_KEY`, and Compose refuses to render them without either. The lease bounds how long a crashed or stalled holder blocks the repository; it is not a wait: a hit or a write returns at once, and a call that outlives the lease is refused and left to its caller's own retry. The operation resolves the values from the environment and the repository id from `get_repo`, then takes `github:repository:<id>:registry-secrets:lease` with `SET NX EX` and a unique token; a held lease refuses at once with `registry_secrets_lease_busy`. The receipt `github:repository:<id>:registry-secrets` (no TTL) holds a `SecretsCipher` encryption of the SHA-256 over a fixed context, the repository id and the three names and values, so Redis never holds a value or an unkeyed digest a password could be checked against; a missing or unusable `SECRETS_ENCRYPTION_KEY` refuses with `registry_secrets_receipt_key_invalid`. A receipt that decrypts to the current digest is a hit: no public-key read and no PUT, result `written=0`. Anything else (absent, not a Fernet token, from another key, decrypting to anything but this digest, another payload or repository) is deleted under the lease before all three are written; the receipt is set only after all three succeeded, giving `written=3`. A changed registry value or a recreated repository therefore writes all three again, and a receipt shared by every writer means the deploy after a scheduler or scaffold write sends none. Every receipt read, deletion and publication, the hit itself and the last await before every PUT attempt (the first and each retry) run one Lua script that checks the caller lock first, when one is given, then the lease token. The optional `guard` runs before that check and its refusal is raised unchanged. A lost caller lock refuses with `registry_secrets_caller_lock_lost`, which the deployer raises as `DeployFenceLost`; a lost lease refuses with `registry_secrets_lease_lost`. A partial write, a lost lock or lease, a Redis, identity or transport failure is never success and publishes nothing. Release compares only the holder's own lease token, on success, failure and cancellation: after a failure or cancellation a release error is logged as `registry_secrets_lease_release_failed` and the original exception is raised, while a release error after a hit or a complete write is raised, so it is never success; a crashed holder's lease expires on its own. The receipt attests what the orchestrator last wrote, not what GitHub holds, and a PUT already in flight when the lock or lease expires is not revoked. After a registry secret is edited outside the orchestrator, invalidate one repository's receipt with `docker compose exec -T redis redis-cli DEL github:repository:<id>:registry-secrets` (`<id>` from `gh api repos/<owner>/<repo> --jq .id`). Every registry writer must run this code: a writer outside the receipt could leave it vouching for stale values, so an upgrade quiesces the old writers before the new ones start ([upgrade runbook](DEPLOY.md#later-po-operation-activate-the-shared-registry-secrets-receipt)).
5.  **CI workflow** (`ci.yml`, on push): lint → test → build images → push to self-hosted Docker registry
6.  **Deploy workflow** (`deploy.yml`, on `workflow_dispatch` from Orchestrator): SCP compose files → write `.env` from DOTENV → pull images → `docker compose up`

**Privilege Separation:**
*   **Infra Service**: Can create/destroy servers (Root access via Ansible). Cannot see App Secrets.
*   **GitHub Actions**: Can deploy apps (SSH User access). Can access App Secrets. Cannot destroy servers.

**Docker Registry**: Self-hosted (`registry:2`) behind Caddy with TLS and basic auth. CI pushes images there, deploy pulls from there. GHCR is not used (GitHub App tokens cannot create org packages).

---

## 4. Summary of Flows

1.  **User creates Project** → Orchestrator creates GitHub Repo + makes its registry secrets (`REGISTRY_*`) current.
2.  **User provides Bot Token** → PO tool `set_project_secret` → encrypted in DB (Fernet).
3.  **Infra Service provisions Server** → Uses L1 Keys (Time4VPS API) for server setup. Ansible playbooks for Docker/firewall/users.
4.  **Scaffolder pushes code** → CI (`ci.yml`, auto on push) → builds Docker images → pushes to self-hosted registry.
5.  **Orchestrator triggers deploy** → DevOps subgraph: environment-contract resolution → DOTENV → GitHub Secrets → `workflow_dispatch deploy.yml` → pull images from registry → `docker compose up`.
6.  **Feature deploy** → the `scheduler-pipeline` PR poller makes the registry secrets current, then merges → push-main CI builds the merge commit's images → the PR poller observes them → `deploy:queue` → re-resolve env → deploy.
