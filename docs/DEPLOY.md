# Production Deployment

## Server Prerequisites

- Ubuntu 22.04+ with Docker Engine 24+ and Docker Compose v2.24+
- `deploy` user with `docker` group membership and sudo access
- Directories: `/opt/codegen_orchestrator` (git clone), `/opt/secrets`, `/opt/backups/orchestrator`
- Git clone: `git clone <repo> /opt/codegen_orchestrator`
- Ports 80/443 open (Caddy handles TLS)

## Managed project deploy target

The provisioner prepares `/opt/services` for the `Server.ssh_user` configured on
the target. It creates that user, adds the orchestrator SSH key and Docker group
membership, then sets `/opt/services` to `root:<ssh_user>` with mode `3770`.

The group write bit lets the deploy workflow create `/opt/services/<project>` on
its first `create` deploy. The sticky bit prevents that user from renaming or
removing a root-owned project root. Existing projects remain `root:root 0755`
(or stricter), so the deploy user cannot write `personal_site` or any other
existing root-owned project directory. New project roots belong to the deploy
user and can be updated by their own feature and fix deploys.

The runtime precheck intentionally does not create directories: `create` must
observe an absent project directory, while `feature` and `fix` require one. The
generated workflow creates the directory only after the `create` precheck has
passed.

Before the next mega deploy, apply the provisioner to adopted target `5vei` so
its `/opt/services` root receives this ownership contract.

## GitHub Secrets

All secrets must be configured in the repository's **production** environment.

### SSH & Server

| Secret | Description |
|--------|-------------|
| `PROD_HOST` | Server IP or hostname |
| `SSH_PRIVATE_KEY` | Deploy key for SSH to prod server (GitHub Actions → prod) |

> **Note:** SSH keys for managed servers are stored per-server in the database
> (encrypted with Fernet). The infra-service generates a key pair during provisioning
> and saves it via the API. No SSH key mounting is needed in docker-compose.

### Database

| Secret | Description |
|--------|-------------|
| `POSTGRES_DB` | Database name |
| `POSTGRES_USER` | Database user |
| `POSTGRES_PASSWORD` | Database password |

### LLM Providers

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPEN_ROUTER_KEY` | OpenRouter API key |
| `PO_LLM_MODEL` | PO agent model name |
| `PO_LLM_BASE_URL` | PO agent LLM base URL |
| `PO_LLM_API_KEY` | PO agent LLM API key |
| `SUMMARIZATION_MODEL` | Summarization model name |
| `SUMMARIZATION_MAX_TOKENS` | Max tokens for summarization |
| `SUMMARIZATION_TRIGGER_TOKENS` | Token threshold to trigger summarization |
| `SUMMARIZATION_MAX_SUMMARY_TOKENS` | Max summary output tokens |

### GitHub Integration

| Secret | Description |
|--------|-------------|
| `GH_APP_ID` | GitHub App ID |
| `GH_APP_PRIVATE_KEY` | GitHub App private key PEM (written to `/opt/secrets/github_app.pem`) |
| `GITHUB_ORG` | GitHub organization name |
| `GITHUB_WEBHOOK_SECRET` | Webhook signature secret |
| `GHCR_TOKEN` | GitHub token with `packages:read` scope (for pulling worker images) |

### Telegram

| Secret | Description |
|--------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `ADMIN_TELEGRAM_IDS` | Comma-separated admin Telegram IDs |
| `TELEGRAM_ID_ADMIN` | Primary admin Telegram ID (for seeding) |

### Encryption & Registry

| Secret | Description |
|--------|-------------|
| `SECRETS_ENCRYPTION_KEY` | Fernet key for encrypting project secrets |
| `ORCHESTRATOR_HOSTNAME` | Public hostname (for Caddy TLS, registry) |
| `GHOST_SERVERS` | Comma-separated IPs of managed servers |
| `REGISTRY_USER` | Docker registry basic auth user |
| `REGISTRY_PASSWORD` | Docker registry password |
| `REGISTRY_PASSWORD_HASH` | Bcrypt hash of registry password (for Caddy) |

### Worker Agents

| Secret | Description |
|--------|-------------|
| `FACTORY_API_KEY` | Factory.ai API key |
| `HOST_CLAUDE_DIR` | Path to `.claude` directory on prod server |
| `HOST_CODEX_HOME` | Path to the dedicated file-backed Codex profile described in `docs/coding-agents.md` |

### Admin UI

| Secret | Description |
|--------|-------------|
| `ADMIN_USER` | Admin panel basic auth username |
| `ADMIN_PASSWORD` | Admin panel basic auth password |

### Observability

| Secret | Description |
|--------|-------------|
| `LANGCHAIN_API_KEY` | LangSmith API key (optional, for tracing) |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key (empty = disabled) |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key |
| `CLICKHOUSE_PASSWORD` | ClickHouse password (for Langfuse analytics) |
| `MINIO_ROOT_USER` | MinIO root user (for Langfuse media storage) |
| `MINIO_ROOT_PASSWORD` | MinIO root password |

## QA Node (Prod Server)

Prod servers are provisioned as QA testing nodes via the `qa_runner` Ansible role (`services/infra-service/ansible/roles/qa_runner/`). This allows the QA consumer to SSH to the server and run Claude Code CLI for post-deploy testing.

**What the role installs**:
- 2GB swap file (Claude Code binary extraction needs ~2GB, OOM on 4GB servers without it)
- Claude Code CLI (standalone binary via `curl -fsSL https://claude.ai/install.sh | bash`)
- Python venv at `/opt/qa-runner/venv` with `telethon` + `httpx`
- `.credentials.json` OAuth session (copied from Ansible controller's `~/.claude/.credentials.json`)
- Optional: `telethon.session` file for Telegram bot testing

**Auto-provisioning**: The role is included in `site.yml` and `provision_software.yml` — new servers get QA capabilities automatically. The `claude_credentials_file` defaults to `~/.claude/.credentials.json` on the Ansible controller.

**Manual re-provisioning** (e.g. after session expiry):
```bash
cd services/infra-service
ANSIBLE_STDOUT_CALLBACK=default ansible-playbook -i ansible/inventories/prod/hosts ansible/playbooks/site.yml --tags qa -e "ansible_user=root"
```

## Deploying

Deploy is triggered manually via GitHub Actions:

1. Go to Actions > "Deploy to Production" > Run workflow
2. The workflow: writes `.env` and secret files, pulls code, builds images, pulls worker images from GHCR, starts services, runs migrations, verifies health

## First-Time Setup

```bash
# On the prod server as deploy user:

# 1. Clone the repo
sudo mkdir -p /opt/codegen_orchestrator
sudo chown deploy:deploy /opt/codegen_orchestrator
git clone git@github.com:<org>/codegen_orchestrator.git /opt/codegen_orchestrator

# 2. Create directories
sudo mkdir -p /opt/secrets /opt/backups/orchestrator
sudo chown deploy:deploy /opt/secrets /opt/backups/orchestrator

# 3. Install DB backup timer
sudo ln -sf /opt/codegen_orchestrator/infra/systemd/orchestrator-backup.service /etc/systemd/system/
sudo ln -sf /opt/codegen_orchestrator/infra/systemd/orchestrator-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orchestrator-backup.timer

# 4. Verify timer
systemctl list-timers orchestrator-backup

# 5. Run first deploy from GitHub Actions
```

## DB Backup

- Automatic: daily at 03:00 via systemd timer
- Manual: `sudo /opt/codegen_orchestrator/infra/scripts/backup-db.sh`
- Location: `/opt/backups/orchestrator/`
- Retention: last 7 backups
- Restore: `gunzip -c backup.sql.gz | docker compose exec -T db psql -U $POSTGRES_USER $POSTGRES_DB`

## Updating

Standard deploys happen via the GitHub Actions workflow. For manual intervention:

```bash
cd /opt/codegen_orchestrator
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --remove-orphans
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api alembic upgrade head
docker image prune -f
```
