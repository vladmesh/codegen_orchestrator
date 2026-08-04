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

The adopted target `vps-273978` received the monitoring baseline on 2026-07-27; see the
section below for the operation. The `/opt/services` ownership contract is applied by the
`deploy_target` role, which the baseline run does not cover — it applies only the
`monitoring` tag.

### Destructive provisioning safety

Time4VPS discovery is default-deny. `TIME4VPS_MANAGED_SERVER_IDS` must contain the comma-separated
provider IDs of the servers this installation is allowed to provision. An absent or empty value
means that no Time4VPS server is managed. Every other discovered server is stored as
`is_managed=false` with status `reserved`; it is visible to inventory reads but never enters a
provisioning trigger.

The same allowlist is checked again by `infra-service` before either Ansible or reinstall work and
once more immediately before an OS reinstall. The `is_managed` database flag and the scheduler
trigger filters are separate guards, so a stale status or a manually published queue message cannot
by itself authorize provisioning.

To adopt a new blank target:

1. Read its immutable provider ID from the Time4VPS account and verify the target by both ID and IP.
2. Set the production GitHub secret `TIME4VPS_MANAGED_SERVER_IDS` to the complete allowlist and run
   the deploy workflow. The workflow rewrites the server `.env`; do not edit it by hand.
3. A brand-new allowlisted provider server enters `pending_setup`. Adding an existing inventory row
   to the allowlist only marks it managed and sends an alert; it does not schedule work.
4. If a verified blank server has no working orchestrator SSH access, request `force-rebuild`
   explicitly through the admin API and watch the provisioning logs.

Never put a personal, development, or already populated server in this list. A failed SSH probe alone
never authorizes reinstall; only an explicit `force-rebuild` request does.

## Monitoring baseline for adopted servers

To install monitoring without reinstalling or running the full provisioning path,
run the supported operation from the infra-service container:

```bash
docker compose exec infra-service python -m src.provisioner.monitoring_baseline SERVER_HANDLE
```

It runs only the `monitoring` tag in `provision_software.yml`, then checks the
target node exporter from the Ansible controller. A failed HTTP check makes the command
fail and does not record the baseline as applied. After a successful run,
`GET /api/servers/SERVER_HANDLE/monitoring-status` shows the baseline timestamp,
the last successful exporter observation and the newest metrics sample. Its
`not_provisioned` state is distinct from a baseline that is waiting for metrics.

`server_unreachable` incidents are sent to administrators by the health checker
when the exporter cannot be fetched, and resolved notifications are sent when it
returns. They also remain visible through `GET /api/servers/SERVER_HANDLE/incidents`.

### Known issue: promtail crash-loops after a baseline run

The `monitoring` role writes a compose file that mounts `/opt/monitoring/promtail.yml`
unconditionally, but renders that file only `when: loki_push_url is defined`. The variable
lives in `ansible/group_vars/all.yml`, which is never loaded: the runner writes its inventory
to a temporary file elsewhere, and `provision_software.yml` loads only `provision_vars.yml`
explicitly. With the variable undefined the template step is skipped, Docker then creates
`/opt/monitoring/promtail.yml` as a *directory*, and promtail restarts forever with
`Unable to parse config: ... is a directory`.

Until the role is fixed, check promtail after a baseline run. `node_exporter` and `cadvisor`
are unaffected, so server metrics keep flowing. On `vps-273978` promtail was stopped and the
stray directory removed on 2026-07-27; the host has no orchestrator-deployed containers to
scrape, so nothing is lost.

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
| `GRAFANA_DB_USER` | Dedicated read-only PostgreSQL role used by Grafana |
| `GRAFANA_DB_PASSWORD` | Password for the Grafana read-only PostgreSQL role |

### Internal Service Authentication

| Secret | Description |
|--------|-------------|
| `INTERNAL_API_KEY` | Random shared credential sent by trusted services as `X-Internal-Key` |

The key says which caller this is, not whose behalf it acts on. A request that also
carries `X-Telegram-ID` — the PO agent's and the bot's do — is judged as that user, so
holding the key does not open a stranger's project, run or admin endpoint. A service call
that names no user is unrestricted, as before. `resolve_actor` in
`services/api/src/dependencies.py` is where that is decided, and the only place that reads
the key's verdict; every access guard asks it.

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
| `TIME4VPS_MANAGED_SERVER_IDS` | Required allowlist of provider IDs the orchestrator may provision or reinstall; empty denies all |
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
| `LOKI_PUSH_USER` | Basic-auth user for remote Promtail writes |
| `LOKI_PUSH_PASSWORD` | Plaintext password configured on managed Promtail clients |
| `LOKI_PUSH_PASSWORD_HASH` | Caddy-compatible bcrypt hash of the Loki push password |
| `GRAFANA_ADMIN_PASSWORD` | Grafana administrator password |

## QA Node (Prod Server)

Prod servers are provisioned as QA testing nodes via the `qa_runner` Ansible role (`services/infra-service/ansible/roles/qa_runner/`). This allows the QA consumer to SSH to the server and run Claude Code CLI for post-deploy testing.

**What the role installs**:
- 2GB swap file (Claude Code binary extraction needs ~2GB, OOM on 4GB servers without it)
- Claude Code CLI (standalone binary via `curl -fsSL https://claude.ai/install.sh | bash`)
- Python venv at `/opt/qa-runner/venv` with `telethon` + `httpx`
- `.credentials.json` OAuth session (copied from Ansible controller's `~/.claude/.credentials.json`)
- `~/.qa-telethon.env` (mode 0600) with `TELETHON_API_ID`, `TELETHON_API_HASH`, `TELETHON_SESSION`
  taken from the orchestrator `.env`. All three are required: Telethon needs api_id/api_hash even
  with an authorized session, so the role fails the play when any of them is empty. The QA prompt
  sources this file — non-interactive SSH reads no profile.

Everything user-scoped is installed for `{{ deploy_user }}` (the server's `ssh_user`), because the
QA consumer connects as that user and calls `claude` through its `$HOME/.local/bin`. The role
verifies the binary by running it as that user, so a failed download fails the play instead of
leaving a server that reports OK and answers QA with exit status 127.

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
