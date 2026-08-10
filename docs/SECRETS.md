# Secrets Management Architecture

The secrets management architecture in Codegen Orchestrator separates the responsibilities between the Orchestrator and the user projects.

## 1. Classification of Secrets

We distinguish three levels of secrets, which have different lifecycle and storage models.

| Level | Description | Examples | Owner | Storage (Master) | Usage |
|---------|----------|---------|----------|-------------------|---------------|
| **L1. Platform** | The infrastructure keys of the Orchestrator itself | `GH_APP_PRIVATE_KEY`, `POSTGRES_URL`, `OPENAI_API_KEY`, `CLOUDFLARE_API_TOKEN` | The Orchestrator's DevOps | K8s Secrets / `.env` | Injected into the service containers (Worker, API, Infra) |
| **L2. Project** | Secrets for running the generated applications | `TELEGRAM_BOT_TOKEN`, `STRIPE_KEY` | The user | **GitHub Repository Secrets** (in the project repo) | CI/CD pipelines (`service_template`), Ansible |
| **L3. User** | The user's personal keys for providers (Future) | User's Cloudflare Key, AWS Key | The user | **Encrypted DB Table** (`user_vault`) | Infra Service (for provisioning on behalf of the user) |

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
*   **Graceful degradation**: when decrypting plaintext values (legacy) — a warning to the log, the value is returned as-is. On the next write it migrates to encrypted (encrypt-on-write).
*   **Lifecycle**:
    1.  The user enters a token (for example, a Telegram Token) through the PO in Telegram.
    2.  PO tool `set_project_secret` → atomic merge via `POST /projects/{id}/config/secrets` (server-side `SELECT FOR UPDATE` locking, handles concurrent writes).
    3.  DevOps subgraph `SecretResolverNode` → decrypt from DB → resolve → encrypt → save back via atomic merge.
*   **Usage**:
    *   The secrets are available decrypted only at runtime (when `decrypt_dict` is called)
    *   In the DB they are always encrypted — even a direct SELECT shows only Fernet tokens

### Level 3: User Secrets (The Provider Accounts) - *Future/Complex*
For the case where the user needs to provision resources in their own account (for example, a VPS on their DigitalOcean).
*   **Storage**: the `user_vault` table (user_id, key, encrypted_value).
*   **Encryption**: Symmetric Key Encryption (AES-GCM), the encryption key is an L1 Secret (`VAULT_MASTER_KEY`).
*   **Usage**: the Infra Service decrypts them "on the fly" before calling the provider (Terraform/Ansible).

---

## 3. Integration with Components

### Infra Service (Provisioning Only)

The `infra-service` is responsible for preparing the "bare metal". It uses **L1 Secrets** only.
*   **SSH Key**: Uses Orchestrator's L1 Private Key to connect to servers.
*   **Provider Keys**: API keys for Time4VPS/DigitalOcean (L1).

It does **NOT** handle Project (L2) secrets. It does not deploy applications.

### Deployment via GitHub Actions

Application deployment is fully delegated to GitHub Actions. This allows secure usage of L2 secrets without exposing them to the Orchestrator's backend.

1.  **DOTENV trick**: Orchestrator collects ALL env vars → builds `.env` content → base64-encodes → stores as single GitHub Secret `DOTENV`. The deploy workflow decodes and writes the file. No per-variable enumeration needed.
2.  **Secret Injection** (two stages):
    *   **Scaffolder**: Sets `REGISTRY_URL`, `REGISTRY_USER`, `REGISTRY_PASSWORD` immediately after repo creation (before first CI push)
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
6.  **Feature deploy** → Developer pushes → CI passes → GitHub webhook → API → `deploy:queue` → re-resolve env → deploy.
