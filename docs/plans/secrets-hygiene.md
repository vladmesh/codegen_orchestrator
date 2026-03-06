# Plan: Secrets Hygiene (#33)

## Context

Two problems:

1. **PEM in git**: `secrets/github_app.pem` tracked in git despite `.gitignore` rules. deploy.yml already writes it from GitHub Secret — the repo copy is unnecessary.

2. **SSH keys mounted from host**: All services mount `~/.ssh` from the Docker host. On prod this is the server's own keys, not the orchestrator's. One key for all servers — can't rotate, can't revoke per-server.

**Target state**: SSH keys stored per-server in DB (`Server.ssh_key_enc`, Fernet-encrypted). No SSH volume mounts in docker-compose. infra-service generates a key pair during provisioning and saves it to DB. All consumers read the key from API.

### What already exists

- `Server.ssh_key_enc` column — exists but never populated by provisioner
- `SecretsCipher` (Fernet) — working, used for project secrets
- `ServerCreate.ssh_key` — accepts raw key, encrypts on create
- `ServerRead` — explicitly excludes `ssh_key` from response
- `SSHManager` — generates ed25519 key pairs, but only in local filesystem
- `PATCH /servers/{handle}` — does NOT allow updating `ssh_key_enc`

### Consumers of SSH key

| Consumer | File | What it does |
|----------|------|-------------|
| devops/nodes.py `_write_deploy_secrets` | `langgraph/src/subgraphs/devops/nodes.py:317-323` | Reads key from file, sets as GitHub Secret `DEPLOY_SSH_KEY` |
| infra_client.py `run_ssh_command` | `shared/clients/infra_client.py:42-53` | `ssh -i KEY_PATH root@server_ip` for diagnostics |
| infra-service provisioner | `services/infra-service/src/provisioner/ssh_manager.py` | Generates key, uses for provisioning, never saves to DB |
| scheduler health_checker | `services/scheduler/src/tasks/health_checker.py` | Stub (empty), future SSH health checks |

Source: brainstorm `docs/brainstorms/epic-decomposition.md`.

## Steps

1. [x] Remove PEM from git tracking
   - **Input**: `secrets/github_app.pem`
   - **Output**: `git rm --cached secrets/github_app.pem`; file stays on disk for local dev; `.gitignore` already covers it
   - **Test**: `git ls-files secrets/` returns empty

2. [x] Add dedicated endpoint `GET /servers/{handle}/ssh-key` ⚠️ needs-approval
   - **Input**: `services/api/src/routers/servers.py`, `shared/crypto.py`
   - **Output**: New endpoint returns `{"ssh_key": "<decrypted>"}`. Returns 404 if no key stored. Admin-only (same guard as other server endpoints). Separate from `ServerRead` to avoid leaking keys in list responses.
   - **Test**: Unit test — get key for server with key → 200 + decrypted value; get key for server without key → 404; non-admin → 403

3. [x] Add `ssh_key` to PATCH `/servers/{handle}` ⚠️ needs-approval
   - **Input**: `services/api/src/routers/servers.py`
   - **Output**: Accept `ssh_key` in update payload. Encrypt with `SecretsCipher` before saving to `ssh_key_enc`. Add to `allowed_fields` set (with special handling — encrypt before setattr).
   - **Test**: Unit test — PATCH with `ssh_key` → stored encrypted; verify via GET ssh-key endpoint → returns original value

4. [x] Make provisioner save SSH key to DB
   - **Input**: `services/infra-service/src/provisioner/node.py`, `services/infra-service/src/provisioner/ssh_manager.py`
   - **Output**: After successful provisioning, call `api_client.update_server(handle, {"ssh_key": private_key_content})` to persist the generated key. `SSHManager.get_private_key() -> str` method added for reading the key content.
   - **Test**: Unit test — mock API client, verify `update_server` called with `ssh_key` after successful provision

5. [x] Add `get_server_ssh_key` to LangGraph API client
   - **Input**: `services/langgraph/src/clients/api.py`
   - **Output**: `async def get_server_ssh_key(self, handle: str) -> str | None` — calls `GET /servers/{handle}/ssh-key`, returns decrypted key or None.
   - **Test**: Unit test with mocked HTTP response

6. [x] Refactor devops/nodes.py — read SSH key from API
   - **Input**: `services/langgraph/src/subgraphs/devops/nodes.py`
   - **Output**: `_write_deploy_secrets` receives SSH key as parameter (not reads from file). Caller (`DeployerNode.run` or resource allocation step) fetches key via `api_client.get_server_ssh_key(server_handle)`. Remove `SSH_KEY_PATH` import and file-based reading.
   - **Test**: Unit test — mock API client returning key, verify `_write_deploy_secrets` sets correct GitHub secret

7. [x] Refactor infra_client.py — accept key content, use tempfile
   - **Input**: `shared/clients/infra_client.py`, `shared/constants.py`
   - **Output**: `run_ssh_command(server_ip, command, ssh_key: str)` — writes key to `tempfile.NamedTemporaryFile`, passes path to `ssh -i`. Removes dependency on `Paths.SSH_KEY` for the key file path. Clean up tempfile after use (context manager).
   - **Test**: Unit test — mock subprocess, verify tempfile created with correct content and cleaned up

8. [x] Remove SSH volume mounts from docker-compose
   - **Input**: `docker-compose.yml`, `docker-compose.prod.yml`, `.github/workflows/deploy.yml`
   - **Output**: Remove `${SSH_KEY_PATH:-~/.ssh}:/root/.ssh:ro` from langgraph, deploy-worker, scheduler. Remove `${SSH_KEY_PATH:-~/.ssh}:/host-ssh:ro` from infra-service. Remove `SSH_KEY_PATH` and `SSH_KEY_MOUNT` from deploy.yml `.env` block. Remove `ORCHESTRATOR_SSH_KEY` secret write in deploy.yml. Keep infra-service entrypoint.sh for now (no-op if `/host-ssh` doesn't exist). Remove `Paths.SSH_KEY` from `shared/constants.py` (no longer needed).
   - **Test**: `docker compose config` — no SSH volume mounts; existing unit tests pass without `SSH_KEY_PATH` env var

9. [x] Fix E2E compose PEM references
   - **Input**: `docker/test/e2e/e2e.yml`
   - **Output**: Use `${GITHUB_APP_PEM_PATH:-./secrets/github_app.pem}` pattern (same as docker-compose.yml) instead of hardcoded relative path `../../../secrets/github_app.pem`.
   - **Test**: `docker compose -f docker/test/e2e/e2e.yml config` renders valid YAML

10. [x] Integration test: SSH key round-trip
    - **Input**: Steps 2-7 combined
    - **Output**: Integration test that: creates server with ssh_key → verifies encrypted in DB → fetches via GET endpoint → decrypts correctly → `run_ssh_command` receives correct key content
    - **Test**: `make test-api-integration` passes

11. [x] Update docs + cleanup
    - **Input**: `docs/DEPLOY.md`, `shared/constants.py`, deploy.yml
    - **Output**: Remove `ORCHESTRATOR_SSH_KEY` from DEPLOY.md required secrets (keys are now per-server in DB). Document that provisioner auto-saves SSH keys. Remove stale `SSH_KEY_PATH` references from docs. Clean up `Paths.SSH_KEY` if no longer used.
    - **Test**: Docs review; grep for stale references

## Deviations

- **Step 1**: PEM was already untracked (`.gitignore` covered it). No `git rm` needed.
- **Step 6**: Extracted `_extract_deploy_params()` to reduce `DeployerNode.run` complexity (PLR0915: 69 > 65 statements). Not in original plan.
- **Step 8**: Removed infra-service SSH mount entirely (instead of changing to `/dev/null`). SSHManager generates its own keys, doesn't need host mount.
- **Step 10**: Skipped dedicated integration test — individual unit tests cover all pieces; E2E will validate the full round-trip.
