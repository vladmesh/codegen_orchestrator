# Production Deployment

## Server Prerequisites

- Ubuntu 22.04+ with Docker Engine 24+ and Docker Compose v2.24+
- `deploy` user with `docker` group membership and sudo access
- Directories: `/opt/codegen_orchestrator` (git clone), `/opt/secrets`, `/opt/backups/orchestrator`
- Git clone: `git clone <repo> /opt/codegen_orchestrator`
- Ports 80/443 open (Caddy handles TLS)

## GitHub Secrets

All secrets must be configured in the repository's **production** environment.

### SSH & Server

| Secret | Description |
|--------|-------------|
| `PROD_HOST` | Server IP or hostname |
| `SSH_PRIVATE_KEY` | Deploy key for SSH to prod server |
| `ORCHESTRATOR_SSH_KEY` | Dedicated SSH key for orchestrator (written to `/opt/secrets/ssh_key`) |

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

### Observability

| Secret | Description |
|--------|-------------|
| `LANGCHAIN_API_KEY` | LangSmith API key (optional, for tracing) |

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
