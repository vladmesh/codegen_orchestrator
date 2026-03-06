# Plan: Secrets Hygiene (#33)

## Context

The PEM file `secrets/github_app.pem` is tracked in git despite being a secret (repo is private but this is bad practice). The deploy.yml already writes it from GitHub Secret to `/opt/secrets/github_app.pem` on prod.

SSH keys are mounted from `~/.ssh` of the host, which is non-portable — on prod the host's SSH directory contains the server's own keys, not the orchestrator's. Deploy.yml already writes `ORCHESTRATOR_SSH_KEY` to `/opt/secrets/ssh_key` and sets `SSH_KEY_PATH` env var, but the code still defaults to `/root/.ssh/id_ed25519`.

Source: brainstorm `docs/brainstorms/epic-decomposition.md` (infrastructure section).

### Current state

**PEM file:**
- `secrets/github_app.pem` — tracked in git (committed before `.gitignore` rules)
- `.gitignore` already has `*.pem` (line 5) and `secrets` (line 50) — but git still tracks it
- `docker-compose.yml` mounts via `${GITHUB_APP_PEM_PATH:-./secrets/github_app.pem}` (5 services)
- `deploy.yml` sets `GITHUB_APP_PEM_PATH=/opt/secrets/github_app.pem` in .env
- `e2e.yml` mounts `../../../secrets/github_app.pem` directly (2 services)
- `shared/clients/github.py:26` reads from `GITHUB_APP_PRIVATE_KEY_PATH` (default `/app/keys/github_app.pem`)

**SSH keys:**
- `docker-compose.yml` mounts `${SSH_KEY_PATH:-~/.ssh}` → `/root/.ssh:ro` (langgraph, deploy-worker, scheduler)
- `infra-service` mounts `${SSH_KEY_PATH:-~/.ssh}` → `/host-ssh:ro`, copies in entrypoint
- `shared/constants.py:13`: `Paths.SSH_KEY = os.getenv("SSH_KEY_PATH", "/root/.ssh/id_ed25519")`
- `devops/nodes.py:317-323` reads SSH key from `Paths.SSH_KEY` to set as GitHub repo secret
- `infra-service/ssh_manager.py:19` defaults to `~/.ssh/id_ed25519`
- On prod: deploy.yml writes orchestrator SSH key to `/opt/secrets/ssh_key` and sets `SSH_KEY_PATH=/opt/secrets/ssh_key`

**Problem:** The `SSH_KEY_PATH` env var serves dual purpose:
1. In docker-compose.yml — it's a **host path** to mount (directory or file)
2. In `Paths.SSH_KEY` / `ssh_manager.py` — it's the **in-container path** to the private key file

On prod, deploy.yml sets `SSH_KEY_PATH=/opt/secrets/ssh_key`. This gets used:
- In docker-compose.yml: mounts `/opt/secrets/ssh_key` (a file) to `/root/.ssh` — this will fail because it mounts a file as a directory
- In constants.py: reads `/opt/secrets/ssh_key` — which is the in-container path, but the file is actually at `/root/.ssh` after the mount

This needs to be split into two variables: one for the host mount source, one for the in-container path.

## Steps

1. [ ] Remove PEM from git tracking
   - **Input**: `secrets/github_app.pem` (tracked file), `.gitignore`
   - **Output**: File removed from git index (but kept locally for dev), `.gitignore` confirms coverage
   - **Test**: `git ls-files secrets/` returns empty; `secrets/github_app.pem` still exists on disk

2. [ ] Fix SSH key dual-variable problem
   - **Input**: `docker-compose.yml`, `docker-compose.prod.yml`, `shared/constants.py`, `services/infra-service/src/provisioner/ssh_manager.py`, `services/infra-service/entrypoint.sh`, `.github/workflows/deploy.yml`, `docs/DEPLOY.md`
   - **Output**:
     - Split `SSH_KEY_PATH` into two env vars:
       - `SSH_KEY_MOUNT` — host path for docker-compose volume mount (default: `~/.ssh` for dev)
       - `SSH_KEY_PATH` — in-container path to the private key file (default: `/root/.ssh/id_ed25519`)
     - `docker-compose.yml`: mount `${SSH_KEY_MOUNT:-~/.ssh}:/root/.ssh:ro`
     - `infra-service`: mount `${SSH_KEY_MOUNT:-~/.ssh}:/host-ssh:ro` (unchanged pattern, new var name)
     - `shared/constants.py`: keep `SSH_KEY_PATH` default `/root/.ssh/id_ed25519`
     - `deploy.yml`: set `SSH_KEY_MOUNT=/opt/secrets` (directory containing `ssh_key` file named `id_ed25519`) OR mount the single file properly
     - `infra-service/ssh_manager.py`: use `Paths.SSH_KEY` instead of hardcoded `~/.ssh/id_ed25519`
   - **Test**: Unit test for `Paths.SSH_KEY` default; unit test for `SSHManager` using `Paths.SSH_KEY`; verify docker-compose config renders correctly

3. [ ] Update deploy.yml secrets layout for SSH
   - **Input**: `.github/workflows/deploy.yml`
   - **Output**:
     - Write SSH key to `/opt/secrets/id_ed25519` (not `ssh_key`) so the directory can be mounted as `/root/.ssh`
     - Set `SSH_KEY_MOUNT=/opt/secrets/ssh` in .env (dedicated dir with just the key)
     - Or: write to `/opt/secrets/ssh/id_ed25519`, set `SSH_KEY_MOUNT=/opt/secrets/ssh`
     - Keep `SSH_KEY_PATH=/opt/secrets/ssh/id_ed25519` for `Paths.SSH_KEY` override
   - **Test**: Manual review of deploy.yml; verify infra-service entrypoint handles single-key directory

4. [ ] Fix E2E compose PEM references
   - **Input**: `docker/test/e2e/e2e.yml`, `.env.test`
   - **Output**: E2E compose uses `GITHUB_APP_PEM_PATH` env var (same pattern as docker-compose.yml) instead of hardcoded relative path. Falls back to `./secrets/github_app.pem` for local dev where the file exists on disk.
   - **Test**: `docker compose -f docker/test/e2e/e2e.yml config` renders correctly

5. [ ] Update documentation
   - **Input**: `docs/DEPLOY.md`
   - **Output**: Document `SSH_KEY_MOUNT` vs `SSH_KEY_PATH` distinction; confirm `GH_APP_PRIVATE_KEY` secret requirement; remove any references to `secrets/github_app.pem` being in the repo
   - **Test**: Review doc accuracy
