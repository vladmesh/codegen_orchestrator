# Plan: Prod Deploy Pipeline (#32)

## Context

Orchestrator needs a working production deployment. Current state:
- `deploy.yml` exists but is incomplete — writes only 6 of ~20 env vars, doesn't build worker images, uses `docker compose pull` (but images aren't in any registry)
- `docker-compose.yml` is dev-oriented: bind-mounts source code, mounts `~/.ssh` from host, references local `secrets/github_app.pem`
- Worker base images (common/claude/factory) are built in CI and pushed to GHCR, but deploy.yml doesn't pull them
- No DB backup strategy
- No `docker-compose.prod.yml` override — prod needs different volume/mount config

What works:
- CI builds and pushes worker images to GHCR (`build-worker-base` job in ci.yml)
- Caddy handles TLS with persistent `caddy-data` volume
- Alembic migrations are solid (19 migrations)
- Health checks on all critical services

Brainstorm source: `docs/brainstorms/epic-decomposition.md` (section "Infrastructure").

Scope decision (from conversation): **Makefile guard on `make nuke` is out of scope** — Makefile is a dev tool, not used on prod. deploy.yml works directly with `docker compose`.

## Steps

1. [x] Create `docker-compose.prod.yml` override
   - **Input**: `docker-compose.yml` (current dev config)
   - **Output**: `docker-compose.prod.yml` that overrides dev config for prod:
     - Remove ALL source bind-mounts (`./services/*/src:/app/src`, `./shared:/app/shared`)
     - Remove `./secrets/github_app.pem` mounts — replace with `/opt/secrets/github_app.pem`
     - Remove `~/.ssh:/root/.ssh:ro` mounts — replace with `/opt/secrets/ssh_key` (dedicated key)
     - Remove `HOST_CLAUDE_DIR` default — must be set explicitly on prod
     - Remove `ports: 8000:8000` on api (Caddy proxies, no direct access)
     - Add `restart: unless-stopped` to all app services
     - Override db defaults: remove `:-orchestrator`, `:-postgres` fallbacks (require explicit env vars)
   - **Test**: `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` validates merged config (manual, documented in deploy.yml comments)

2. [x] Create `infra/scripts/pull-worker-images.sh`
   - **Input**: GHCR image tags, worker-manager config (`image_builder.py` expects `worker-base-claude:latest` etc.)
   - **Output**: Script that pulls worker images from GHCR and retags to local names:
     - `ghcr.io/.../worker-base-common:latest` -> `worker-base-common:latest`
     - `ghcr.io/.../worker-base-claude:latest` -> `worker-base-claude:latest`
     - `ghcr.io/.../worker-base-factory:latest` -> `worker-base-factory:latest`
     - Accepts `GHCR_TOKEN` and `GHCR_OWNER` as env vars
     - Logs progress, fails fast on errors
   - **Test**: Unit test — script is simple enough to verify by review. CI validates images build correctly (existing `build-worker-base` job).

3. [x] Create `infra/scripts/backup-db.sh` + systemd timer
   - **Input**: PostgreSQL container name, backup path
   - **Output**:
     - `infra/scripts/backup-db.sh` — runs `docker compose exec -T db pg_dump` to `/opt/backups/orchestrator/`
     - Filename: `orchestrator_YYYY-MM-DD_HH-MM.sql.gz` (gzipped)
     - Retains last 7 daily backups (deletes older)
     - `infra/systemd/orchestrator-backup.service` + `orchestrator-backup.timer` (daily at 03:00)
   - **Test**: Script runs successfully against local dev stack (`make up` + run script, verify .sql.gz created). Timer syntax validated with `systemd-analyze verify`.

4. [x] Rewrite `deploy.yml` — full production deploy workflow
   - **Input**: Current `deploy.yml`, new `docker-compose.prod.yml`, `pull-worker-images.sh`
   - **Output**: Updated `.github/workflows/deploy.yml`:
     - **Step 1**: SSH — write `.env` with ALL required env vars (not just 6)
     - **Step 2**: SSH — write `github_app.pem` from secret to `/opt/secrets/`
     - **Step 3**: SSH — `git pull origin main`
     - **Step 4**: SSH — `docker compose -f docker-compose.yml -f docker-compose.prod.yml build`
     - **Step 5**: SSH — run `pull-worker-images.sh` (pull + retag worker images from GHCR)
     - **Step 6**: SSH — `docker compose -f ... up -d --remove-orphans`
     - **Step 7**: SSH — `docker compose exec -T api alembic upgrade head`
     - **Step 8**: SSH — `docker image prune -f`
     - Add all required GitHub Secrets to `env:` section with comments
     - Add health check after deploy (curl /health with retry)
   - **Test**: `act` dry-run or manual review of YAML syntax. Real test = step 5 (manual deploy to prod).

5. [x] Manual prod deploy test + document required GitHub Secrets
   - **Input**: All previous steps merged, prod server access
   - **Output**:
     - Document in `docs/DEPLOY.md`: list of required GitHub Secrets, server prerequisites (Docker, deploy user, /opt/secrets/, /opt/backups/), first-time setup steps
     - Install systemd timer for DB backup on prod
     - Run `workflow_dispatch` deploy and verify all services healthy
   - **Test**: All services respond to health checks. `docker compose ps` shows all containers running. DB backup script produces valid dump.

## Notes

- Worker-manager references images by local tag (`worker-base-claude:latest`). Changing this to GHCR tags would require code changes + config. Simpler to pull-and-retag.
- `#33 Secrets Hygiene` (next in queue) will handle removing PEM from git and dedicated SSH key. This plan assumes those secrets exist in GitHub Secrets but doesn't create them.
- Redis persistence: `redis_data` volume survives restarts. For prod resilience, consider enabling Redis AOF in a future task.

## Deviations

- **Step 1**: Instead of removing dev bind-mounts (impossible with compose list merge semantics), parameterized secret paths (`SSH_KEY_PATH`, `GITHUB_APP_PEM_PATH`) in the base `docker-compose.yml` with dev defaults. Prod sets these via `.env`. Source bind-mounts are harmless on prod (same code from git checkout overlays baked-in image code). Prod override uses `!reset` for ports only (requires Compose 2.24+).
- **Step 5**: Manual prod deploy test deferred — DEPLOY.md created with full instructions. Actual deploy will happen when prod server is provisioned and GitHub Secrets are configured.
