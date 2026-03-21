---
id: bs-214a5804
status: draft
title: "Worker DB Migration — Recurring DNS Bugs"
created_at: 2026-03-07T12:37:05.623309Z
---

# Worker DB Migration Failure — Recurring Bug Analysis

Status: BRAINSTORM
Created: 2026-03-07

## Problem Statement

"Database migration could not be generated" — keeps recurring despite 10+ fixes across 7 iterations. Latest occurrence: live user run (not E2E). `getent hosts db` returns nothing from within worker context after `orchestrator dev-env start-infra db` succeeds.

## Bug History (10 fixes, 2026-02-20 to 2026-03-05)

| # | Commit | Date | Root Cause | Fix |
|---|--------|------|------------|-----|
| 1 | `6530d68` | 02-20 | Initial architecture — DNS collision, worker on same network as orchestrator db | Dual-network topology (codegen_internal + dev_proj_*) |
| 2 | `d67b22b` | 02-20 | Compose file discovery, flag handling, ENV precedence, directory isolation | Fixed -f flag parsing, .env loading, cwd handling |
| 3 | `39e1da7` | 02-24 | DinD: files written in worker not visible to worker-manager filesystem | Read compose files via `docker exec cat` |
| 4 | `c96f291` | 02-24 | DOCKER_NETWORK override ignored in CI | Respect DOCKER_NETWORK env var |
| 5 | `1764bab` | 03-02 | Workspace path mismatch (project_id vs worker_id) | Added workspace_dir param, Redis meta lookup |
| 6 | `26a367f` | 03-02 | Wrong default compose files (docker-compose.yml vs infra/compose.base.yml) | Default to service-template layout |
| 7 | `143b9a2` | 03-02 | CRITICAL: 4 bugs — file discovery, flag injection, env var leak, DNS collision | Fixed all 4; added project-db alias workaround |
| 8 | `e133e56` | 03-03 | Workers on codegen_internal could resolve orchestrator's `db` | Added codegen_worker network, physical isolation |
| 9 | `12787c4` | 03-04 | Stale worker-manager image after network isolation commit | Bake SOURCE_HASH label, staleness detection |
| 10 | `3138d87` | 03-05 | Missing migration troubleshooting guidance | Updated INSTRUCTIONS.md |

## Current Architecture

```
codegen_internal              codegen_worker           dev_proj_{worker_id}
+-- db (postgres)            +-- redis                +-- project db (postgres:16)
+-- langgraph                +-- api                  +-- project redis (if needed)
+-- deploy-worker            +-- worker-manager       |
+-- scheduler                +-- worker container ----+
+-- infra-service            |
+-- telegram_bot             |
+-- caddy, registry          |
```

Worker container networks: `codegen_worker` + `dev_proj_{worker_id}`
Orchestrator db: only on `codegen_internal` (invisible to workers) -- CORRECT

## How Migrations Run Today

### Flow: `orchestrator dev-env start-infra db`

1. CLI reads WORKER_ID env var
2. HTTP POST to worker-manager: `/api/worker/{worker_id}/infra/compose`
3. compose_runner.py builds command:
   ```
   docker compose --project-name worker_{id}
     --env-file /path/.env
     -f infra/compose.base.yml
     -f infra/compose.dev.yml
     -f /path/.codegen-network.yml   <-- redirects default network to dev_proj_{id}
     up -d --wait db
   ```
4. db container starts on `dev_proj_{worker_id}` with DNS alias `db`

### Flow: `make migrate` / `make makemigrations`

From service-template Makefile (runs DIRECTLY in worker container, NOT via compose run):
```makefile
migrate:
    PYTHONPATH=. services/backend/.venv/bin/alembic -c services/backend/migrations/alembic.ini upgrade head

makemigrations:
    PYTHONPATH=. services/backend/.venv/bin/alembic -c services/backend/migrations/alembic.ini revision --autogenerate -m "$(name)"
```

Alembic reads DATABASE_URL from `.env`: `postgresql+asyncpg://postgres:postgres@db:5432/service`

The worker container must resolve `db` on `dev_proj_{worker_id}` network.

### Flow: `orchestrator dev-env compose run ...` (integration tests)

Goes through compose proxy, same network override applied. Creates one-off container on `dev_proj_{worker_id}`.

## Root Cause Hypotheses

### H1: `docker compose up` container DNS aliases not visible to non-compose containers

The db container is started by `docker compose up` which registers `db` as a `--network-alias` on `dev_proj_{worker_id}`. The worker container was connected to the same network via raw Docker API (`docker.connect_network()`), not via compose. Docker DNS aliases are network-scoped and SHOULD be visible to all containers on that network — but is this guaranteed in all Docker versions?

**Test**: From worker container, run `getent hosts db` and `dig db` after `start-infra db`. If both fail, the alias isn't visible.

### H2: Network connected but DNS resolver not updated

The worker container was connected to `dev_proj_{worker_id}` at creation time (before any infra started). Docker's embedded DNS resolver might cache the negative lookup. When db starts later, the cached "NXDOMAIN" persists.

**Test**: Restart the worker container's DNS or run `nscd -i hosts` (if available). Or disconnect/reconnect the network.

### H3: Race condition — `--wait` returns before DNS propagates

`docker compose up -d --wait db` waits for the healthcheck to pass. But there might be a gap between "container healthy" and "DNS alias fully propagated on the network". The agent runs `make migrate` immediately after `start-infra` returns.

**Test**: Add a `sleep 2` after `start-infra` and see if DNS resolves.

### H4: Network name mismatch (worker_id vs project_id)

Worker created with `worker_id=X`. But if the workspace uses `project_id=Y`, and compose_runner derives network from worker_id, the network override points to `dev_proj_X`. But if the worker was created with a different ID mapping... Check Redis meta for consistency.

### H5: `dev_proj_{id}` network recreated between worker creation and start-infra

If `reset-infra` or some cleanup runs between worker creation and `start-infra`, the network gets destroyed and recreated. The worker container is still connected to the OLD network object (by ID), while db starts on the NEW network (same name, different ID).

**Test**: `docker network inspect dev_proj_{id}` — check if both worker and db containers are listed.

### H6: Compose project isolation of DNS aliases (Docker version-dependent)

Some Docker Compose versions scope service DNS aliases to the compose project. Containers outside the project (like the worker) can't resolve service names even on the same network. The worker container isn't part of the `worker_{id}` compose project.

**Test**: From within a `docker compose run` container (same project as db), does `getent hosts db` work? If yes, this confirms project-scoped DNS.

### H7 (MOST LIKELY): `docker compose run` creates its own network despite override

When `orchestrator dev-env compose run <service> <cmd>` executes, compose may create a `worker_{id}_default` network (compose's default project network) even with the external network override. If the override file isn't loaded correctly, or if compose interprets it differently for `run` vs `up`, the one-off container lands on `worker_{id}_default` instead of `dev_proj_{id}`.

**Test**: After a `compose run`, check `docker network ls | grep worker_` for unexpected networks.

## Proposed Solutions

### S1: Run alembic directly in the worker container (bypass compose run entirely)

The service-template Makefile ALREADY does this: `make migrate` and `make makemigrations` run alembic directly, not via compose run. If the bug is about compose run containers, we need to figure out WHO is running compose run for migrations. Possible culprits:
- The coding agent ignoring INSTRUCTIONS.md and running `docker compose run` directly
- Some other make target using compose run
- Integration test flow using compose run

**Action**: Add logging/audit of what commands the agent actually runs during migration failures.

### S2: Connect db container to worker's network with explicit alias

Instead of relying on compose DNS aliases, explicitly connect the db container to the worker's network with a known alias after `start-infra`:

```python
# After compose up succeeds:
db_container = await docker.get_container(f"worker_{worker_id}-db-1")
await docker.connect_network(f"dev_proj_{worker_id}", db_container.id, aliases=["db"])
```

This ensures the alias is registered at the Docker level, not just compose level.

### S3: Use container IP directly (bypass DNS entirely)

After `start-infra db`, resolve the db container's IP and inject it into `.env`:

```python
# In compose_runner or as post-start-infra hook:
db_ip = docker.inspect(db_container)["NetworkSettings"]["Networks"][dev_network]["IPAddress"]
# Patch .env: POSTGRES_HOST=<ip>
```

**Pro**: Eliminates DNS entirely.
**Con**: Fragile if container restarts, and violates the "don't patch .env" principle from fix #8.

### S4: Use `docker compose exec` instead of `docker compose run`

If the issue is specifically with `run` creating containers on wrong networks, switch to `exec` where possible. `exec` runs in an existing container (no new network assignment). Requires the service to already be running (`compose up` first).

For migrations: `docker compose exec backend alembic upgrade head` (requires backend to be running).

### S5: Add DNS verification step to start-infra

After `start-infra` completes, verify DNS resolution from the worker container before returning success:

```python
# In compose_runner or as post-hook in start_infra:
exit_code, output = await docker.exec_in_container(
    worker_container_name, "getent hosts db"
)
if exit_code != 0:
    # DNS not resolving — try reconnecting network
    await docker.disconnect_network(dev_network, worker_container_id)
    await docker.connect_network(dev_network, worker_container_id)
    # Retry
```

**Pro**: Self-healing, catches the issue at the source.
**Con**: Adds complexity, may mask deeper issues.

### S6: Single-network approach — put worker infra on codegen_worker

Instead of creating per-worker `dev_proj_{id}` networks, run project infra directly on `codegen_worker` with unique container names (e.g., `db-{worker_id}`). Update `.env` to use `db-{worker_id}` as POSTGRES_HOST.

**Pro**: Eliminates multi-network DNS complexity.
**Con**: No isolation between workers; DNS collision risk if multiple workers have `db`.

### S7: Sidecar approach — run db inside the worker container

Instead of separate containers, run PostgreSQL as a sidecar process inside the worker container (or use a pre-configured socket). The worker connects via localhost or Unix socket.

**Pro**: No network/DNS issues at all.
**Con**: Major architecture change, resource management, violates container separation.

## Diagnostic Checklist (for next occurrence)

When the bug occurs again, capture:

1. `docker network inspect dev_proj_{worker_id}` — are both worker AND db containers listed?
2. `docker inspect worker-{worker_id} | jq '.[0].NetworkSettings.Networks'` — what networks is the worker on?
3. `docker inspect worker_{worker_id}-db-1 | jq '.[0].NetworkSettings.Networks'` — what networks is db on?
4. `getent hosts db` from inside worker container
5. `nslookup db` from inside worker container
6. `docker network ls | grep -E "dev_proj|worker_"` — any unexpected networks?
7. Worker-manager logs: `docker logs codegen_orchestrator-worker-manager-1 | grep compose_run`
8. Redis meta: `redis-cli hgetall worker:meta:{worker_id}`

## Recommendation

**Immediate**: Implement S5 (DNS verification + self-healing reconnect in start-infra flow). This is low-risk, catches the failure early, and auto-recovers.

**Short-term**: Implement S2 (explicit alias registration) as defense-in-depth. Don't rely solely on compose-level DNS aliases.

**Investigation**: Add the diagnostic checklist as automated capture on migration failure. We've been fixing symptoms for 10 iterations without definitive root cause data.

## Action Items

- [ ] Triage: Create backlog task for S5 (DNS verification in start-infra)
- [ ] Triage: Create backlog task for S2 (explicit alias registration)
- [ ] Triage: Create backlog task for diagnostic capture on migration failure
- [ ] Investigate: Reproduce on current codebase with diagnostic checklist