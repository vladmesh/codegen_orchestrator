# Platform auth admin API

The `deploy-worker` container runs `python -m src.consumers.deploy`, including the devops graph's
secret resolver and auth admin client. Only this service joins external `codegen-orch-link`, and
only through `docker-compose.prod.yml`. The stand replaces the inherited network map and caller
attachments; dev uses the base networks. Neither contour needs the platform network to start.
The client uses `http://auth:8000/admin/v1/*`; the admin API has no public route.

The platform repository's [deploy/NETWORK.md, Cross-stack calls](https://github.com/vladmesh/codegen-platform-services/blob/main/deploy/NETWORK.md#cross-stack-calls)
names the edge as `orchestrator langgraph` to `auth:8000` on `codegen-orch-link` (admin API only).
Here that LangGraph deploy consumer runs in `deploy-worker`, rather than the `langgraph` container.
The platform [deploy/RUNBOOK.md, Secrets](https://github.com/vladmesh/codegen-platform-services/blob/main/deploy/RUNBOOK.md#secrets)
has the `auth_admin_token` rotation entry and names the orchestrator env copy as a holder.

## One-time setup

These are operator actions after merge. On h01o use the recorded rootless Docker daemon shared
by both stacks, as `vlad`, with the production host override described in [DEPLOY.md](../DEPLOY.md#db-backup).
Do not create a second network on another daemon.

```bash
export DOCKER_HOST=unix:///run/user/1001/docker.sock
docker network inspect codegen-orch-link --format '{{.Name}} internal={{.Internal}}'
```

Expect `codegen-orch-link internal=true`. The platform deploy job creates this external network
with `--internal`. If it is absent or not internal, resolve that through the platform deployment
procedure before deploying the orchestrator.

In this repository's production GitHub Environment, set `PLATFORM_AUTH_ADMIN_URL` to
`http://auth:8000` and `PLATFORM_AUTH_ADMIN_TOKEN` to the platform's current `auth_admin_token`
from its protected secret file. Transfer the value through the approved secret-management
channel without displaying it or putting it in shell history. Set neither secret for stand.
The production deploy workflow writes both to its private `.env`; the base `env_file: .env`
passes them to `deploy-worker`. Hand edits to the host `.env` are overwritten by the next deploy.

Deploy the merged, released orchestrator revision with `environment=production`. Compose
requires the external network to exist, and joins `deploy-worker` to it alongside `internal`.

## Token rotation

1. Follow the platform RUNBOOK's `auth_admin_token` entry: replace its protected secret file,
   then force-recreate platform `auth` so it reads the new value. Record the rotation, never
   the value, in the host change log. Do not rotate `auth_pepper` for an admin token change.
2. Copy the new value into the orchestrator production GitHub Environment secret
   `PLATFORM_AUTH_ADMIN_TOKEN`. Keep `PLATFORM_AUTH_ADMIN_URL=http://auth:8000`.
3. Redeploy the current released orchestrator revision through the production workflow. It
   rewrites `.env` and recreates the caller to read the new environment. For a caller-only
   recreation after the workflow has written the new `.env`, use the same deployed image
   override and h01o mounts:

   ```bash
   cd /home/vlad/codegen_orchestrator
   docker compose -p codegen_orchestrator \
     -f docker-compose.yml -f docker-compose.prod.yml \
     -f /home/vlad/codegen-h01o.override.yml \
     -f deployed-service-images.compose.yml \
     up -d --no-deps --force-recreate deploy-worker
   ```

4. Verify from the caller as below. During the interval between steps 1 and 3, deploys may
   fail with `platform_auth_unauthorized`; finish updating both holders before retrying.

## Verify auth and log collection

Deploy a product whose production environment contract declares a `platform_key` and
`platform_base_url`. Verify successful deploy and registration without a user-secret prompt.
For a read-only check against an already registered product, set `ORCHESTRATOR_PROJECT_ID` to
that product's orchestrator project ID, then run on h01o with the daemon above:

```bash
cd /home/vlad/codegen_orchestrator
docker compose -p codegen_orchestrator \
  -f docker-compose.yml -f docker-compose.prod.yml \
  -f /home/vlad/codegen-h01o.override.yml \
  -f deployed-service-images.compose.yml \
  exec -T deploy-worker python - "$ORCHESTRATOR_PROJECT_ID" <<'PY'
import hashlib
import os
import sys

import httpx
import structlog

project_id = sys.argv[1]
if not project_id:
    raise SystemExit("ORCHESTRATOR_PROJECT_ID is required")
product_id = "orch-" + hashlib.sha256(project_id.encode()).hexdigest()[:58]
url = os.environ["PLATFORM_AUTH_ADMIN_URL"].rstrip("/")
token = os.environ["PLATFORM_AUTH_ADMIN_TOKEN"]
if not url or not token:
    raise SystemExit("platform_service_unconfigured")
try:
    response = httpx.get(
        f"{url}/admin/v1/products/{product_id}",
        headers={"Authorization": "Bearer " + token},
        timeout=15,
        follow_redirects=False,
    )
except httpx.RequestError:
    raise SystemExit("platform_auth_unavailable") from None
structlog.get_logger().info("platform_admin_verification", http_status=response.status_code)
raise SystemExit(0 if response.status_code == 200 else 1)
PY
```

Expect HTTP 200. This reads the token inside the caller, never in command arguments, output or
response-body logs. Do not enable shell tracing, dump `.env` or render Compose's resolved env
to terminal during verification. A 404 means the product ID is not registered; choose a product
that has deployed with a platform key.

After production deploy, generate a platform log line through the check above or a platform
request. In the orchestrator Grafana's Loki Explore, select a time range including that request
and query:

```logql
{compose_project="codegen_platform"}
```

Expect lines from platform containers. Promtail discovers both `codegen_platform` and
`codegen_orchestrator` on the same Docker daemon, preserves `compose_service`, and labels
`compose_project` so their service names remain distinguishable. Worker discovery is retained.
If no lines appear, check the platform project label, Promtail's Docker socket mount in the
h01o override, and Promtail-to-Loki delivery. A passing config test alone does not prove live
ingestion; retain the query result with the post-merge deployment evidence.

## Failure signatures

| Diagnostic | Meaning and action |
|------------|--------------------|
| `platform_service_unconfigured` | Admin URL or token is missing. Set both production Environment secrets and redeploy; do not ask the product user for a key. |
| `platform_auth_unauthorized` | Auth returned 401/403, typically a stale token. Complete platform-first rotation, update the orchestrator secret and redeploy. |
| `platform_auth_unavailable` | DNS/connectivity failure, timeout, 429 or 5xx. Check the shared daemon/network and auth health; the deploy supervisor retries under its configured bound. |

A missing external network fails Compose startup before issuance. `platform_auth_configuration_invalid`
or `platform_auth_request_refused` indicates invalid URL configuration or another admin response
refusal; repair the configuration rather than supplying a user secret.
