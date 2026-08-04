# Resource Management & Secrets Isolation

> Resources are allocated through the `ResourceAllocator` in the Engineering Worker. This document describes the secret isolation pattern used in the system.

## Principle: the LLM never sees secrets

```
┌─────────────────────────────────────────────────────────────┐
│                    LangGraph State                          │
│  (this is what the agents see - Product Owner)              │
│                                                             │
│  allocated_resources: {                                     │
│      "server_handle:8000": {                                │
│          "port": 8000,                                      │
│          "server_handle": "prod_vps_1",  ← a name, not IP   │
│          "service_name": "backend"                          │
│      }                                                      │
│  }                                                          │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                  Resource allocation                        │
│                                                             │
│  Functional part (ResourceAllocatorNode in Engineering):    │
│  - Automatically allocates ports and servers through the API│
│  - Reuses the logic from `tools/allocator.py`               │
│  - Does NOT use an LLM (fully deterministic)                │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   Secrets Storage                           │
│  project.config.secrets (PostgreSQL, Fernet-encrypted)      │
│                                                             │
│  Example (a telegram token):                                │
│  In the DB: "gAAAAA..."  ← Fernet-encrypted at rest         │
└─────────────────────────────────────────────────────────────┘
```

## Current implementation: PostgreSQL + Fernet encryption

Secrets are stored in the `config.secrets` field of the `Project` model, Fernet-encrypted at rest:

```python
from shared.crypto import decrypt_dict, encrypt_dict

# Read: decrypt after receiving from the API
config_secrets = project_spec.get("config", {}).get("secrets", {})
config_secrets = decrypt_dict(config_secrets) if config_secrets else {}

# Write: encrypt before sending to the API
config["secrets"] = encrypt_dict(secrets)
await api_client.patch(f"/projects/{project_id}", json={"config": config})
```

The encryption key: the env var `SECRETS_ENCRYPTION_KEY` (a Fernet key). If it is missing — a `RuntimeError` on the first encrypt/decrypt call.

## Secret types

The DevOps subgraph classifies environment variables into three types:

| Type | Description | Example |
|-----|----------|--------|
| `infra` | Generated automatically | `DATABASE_URL`, `REDIS_URL` |
| `computed` | Computed from the context | `APP_NAME`, `PORT` |
| `user` | Required from the user | `TELEGRAM_BOT_TOKEN`, `API_KEY` |

## How the Product Owner deals with secrets

The Product Owner agent asks the user for secrets directly (for example, `TELEGRAM_BOT_TOKEN`) if they are required by the selected modules.
The PO calls the `set_project_secret` tool, which stores the token in the DB, encrypting it with Fernet right away. The PO never sees or generates any infrastructure keys (SSH, DB).

## Server Management

The system supports a hybrid infrastructure synchronized with the provider (Time4VPS).

1.  **Source of Truth**: the database (the `api` service).
    *   A background worker (`server_sync.py`) polls the Time4VPS API every minute.
    *   New allowlisted servers are added as `pending_setup`; every other new server is added as
        inventory-only `reserved`.
    *   Servers absent from the provider response are marked as `unreachable`.

2.  **Access to the Time4VPS API is restricted by an address list** on the provider's side (the personal
    account, API access). The login can be correct, but a request from a disallowed address gets a `401` with the body
    `{"error":["ipnotallowed","unauthorized"]}`. When the server IP, the egress route or the scheduler's
    location changes, this is the first thing to check, before the credentials. The response body is written to the log
    as the `time4vps_http_error` event; a wrong login gives `{"error":["wronglogin","unauthorized"]}`.

    A repeated refusal raises a single `provider_api_unavailable` incident (without a `server_handle`,
    since this is a failure of a platform dependency, not of a server; an empty `server_handle` is allowed only for
    this type, the rest are bound to a server and without one are rejected with 422, otherwise the
    deduplication by the `(server_handle, incident_type)` index breaks) and a single alert to the admins. The incident is closed
    automatically once the provider responds again. A cycle that failed to read the provider writes
    `server_sync_incomplete` at the error level and does not report zero counters as a successful synchronization.

3.  **Explicit management allowlist**:
    *   `TIME4VPS_MANAGED_SERVER_IDS` contains the immutable provider IDs this installation may
        provision. Missing or malformed configuration fails closed.
    *   Every other newly discovered provider server is inventory-only
        (`is_managed=False`, `status=reserved`). Demotion preserves operational status but stops
        health/allocation/provisioning consumers through `is_managed=False`.
    *   Existing rows are never auto-provisioned when added to the allowlist; destructive reinstall
        also requires an explicit `force-rebuild` request.
    *   The scheduler and infra-service both enforce the same policy, and the reinstall operation
        repeats it at the provider API boundary.
    *   A stale scheduled row that no longer passes policy is moved to `reserved` and produces one
        administrator alert instead of being retried indefinitely.

## GitHub App & Secrets

A GitHub App is used to work with GitHub (creating repositories, managing workflows).

| Secret Name | Description | Where it is stored |
|-------------|----------|--------------|
| `GH_APP_ID` | The App ID of the Project-Factory-Keeper application | GitHub Secrets |
| `GH_APP_PRIVATE_KEY` | The Private Key (.pem) for signing the JWT | GitHub Secrets |

**Local development:**
- `GITHUB_APP_ID` → `.env`
- The private key → `~/.gemini/keys/github_app.pem` (mounted in docker-compose)

**Production:**
- The secrets are written to the server through the CI/CD workflow
- The path in production: `/opt/secrets/github_app.pem`

## Worker Garbage Collection

For parallel workers (see [docs/parallel-workers.md](parallel-workers.md)) the system creates temporary resources (workspaces, networks, containers) on the host.

1. **The lifecycle**:
   * A worker gets a workspace directory (pre-scaffolded: `/data/workspaces/{repo_id}/`, ephemeral: `/tmp/codegen/workspaces/{worker_id}/`) and an isolated Docker network `dev_proj_<worker_id>`.
   * The agent calls the compose proxy through `curl $WORKER_MANAGER_URL/api/worker/$WORKER_ID/infra/compose` to manage sidecars inside that namespace.
2. **Garbage Collection**:
   * Explicit removal: on completion LangGraph calls `delete_worker` on `worker-manager`, which removes the containers, the network and the space on disk.
   * Background garbage collection (GC): the `scheduler` triggers GC in `worker-manager` every 30 minutes. The `WorkerManager.garbage_collect_orphaned_resources()` method finds "orphaned" worker containers, `dev_proj_*` networks and directories on disk (matching them against the active `worker:status:*` keys in Redis) and removes them, protecting the system from leaks after crashes or OOM events.

## See also

- [SECRETS.md](SECRETS.md) — the secret management architecture (the L1/L2/L3 levels)
- [secrets-vault-implementation.md](tasks/secrets-vault-implementation.md) — a historical plan (superseded by Fernet encryption in `shared/crypto.py`)
