# Secrets Management Architecture

Архитектура управления секретами в Codegen Orchestrator разделяет ответственности между Оркестратором и Пользовательскими проектами.

## 1. Classification of Secrets

Мы выделяем три уровня секретов, которые имеют разные модели жизненного цикла и хранения.

| Уровень | Описание | Примеры | Владелец | Хранение (Master) | Использование |
|---------|----------|---------|----------|-------------------|---------------|
| **L1. Platform** | Инфраструктурные ключи самого Оркестратора | `GH_APP_PRIVATE_KEY`, `POSTGRES_URL`, `OPENAI_API_KEY`, `CLOUDFLARE_API_TOKEN` | DevOps Оркестратора | K8s Secrets / `.env` | Внедряются в контейнеры сервисов (Worker, API, Infra) |
| **L2. Project** | Секреты для запуска сгенерированных приложений | `TELEGRAM_BOT_TOKEN`, `STRIPE_KEY` | Пользователь | **GitHub Repository Secrets** (в репо проекта) | CI/CD пайплайны (`service_template`), Ansible |
| **L3. User** | Личные ключи пользователя для провайдеров (Future) | User's Cloudflare Key, AWS Key | Пользователь | **Encrypted DB Table** (`user_vault`) | Infra Service (для провижининга от имени юзера) |

---

## 2. Detailed Strategy

### Level 1: Platform Secrets (The Orchestrator)
Эти секреты необходимы для функционирования самой платформы.
*   **Storage**: Переменные окружения.
*   **Access**: Читаются при старте сервисов (`os.getenv`).
*   **Repo**: Хранятся в `.env` (локально) или в Secret Manager платформы хостинга.

### Level 2: Project Secrets (The Generated App)
Ключевой момент: **Секреты зашифрованы at rest в PostgreSQL** (Fernet encryption).
*   **Storage**: `project.config.secrets` (JSONB) — все значения зашифрованы Fernet-токенами (`gAAAAA...`).
*   **Encryption**: `shared/crypto.py` — `SecretsCipher` читает `SECRETS_ENCRYPTION_KEY` из env. `encrypt_dict`/`decrypt_dict` шифруют/дешифруют все значения в dict.
*   **Graceful degradation**: При расшифровке plaintext-значений (legacy) — warning в лог, значение возвращается as-is. При следующей записи мигрирует в encrypted (encrypt-on-write).
*   **Lifecycle**:
    1.  Пользователь вводит токен (например, Telegram Token) через PO в Telegram.
    2.  PO tool `set_project_secret` → decrypt existing → add new → encrypt all → PATCH to API.
    3.  DevOps subgraph `SecretResolverNode` → decrypt from DB → resolve → encrypt → save back.
*   **Usage**:
    *   Secrets доступны в расшифрованном виде только в runtime (при вызове `decrypt_dict`)
    *   В БД всегда зашифрованы — даже при прямом SELECT видны только Fernet-токены

### Level 3: User Secrets (The Provider Accounts) - *Future/Complex*
Если пользователю нужно провижить ресурсы на своём аккаунте (например, VPS на его DigitalOcean).
*   **Storage**: Таблица `user_vault` (user_id, key, encrypted_value).
*   **Encryption**: Symmetric Key Encryption (AES-GCM), ключ шифрования — это L1 Secret (`VAULT_MASTER_KEY`).
*   **Usage**: Infra Service расшифровывает "на лету" перед вызовом провайдера (Terraform/Ansible).

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

**Docker Registry**: Self-hosted (`registry:2`) behind Caddy with TLS and basic auth. CI pushes images there, deploy pulls from there. GHCR is not used (GitHub App tokens cannot create org packages — see [investigation](./investigations/ghcr-403-app-token.md)).

---

## 4. Summary of Flows

1.  **User creates Project** → Orchestrator creates GitHub Repo + sets registry secrets (`REGISTRY_*`).
2.  **User provides Bot Token** → PO tool `set_project_secret` → encrypted in DB (Fernet).
3.  **Infra Service provisions Server** → Uses L1 Keys (Time4VPS API) for server setup. Ansible playbooks for Docker/firewall/users.
4.  **Scaffolder pushes code** → CI (`ci.yml`, auto on push) → builds Docker images → pushes to self-hosted registry.
5.  **Orchestrator triggers deploy** → DevOps subgraph: env analysis → secret resolution → DOTENV → GitHub Secrets → `workflow_dispatch deploy.yml` → pull images from registry → `docker compose up`.
6.  **Feature deploy** → Developer pushes → CI passes → GitHub webhook → API → `deploy:queue` → re-resolve env → deploy.
