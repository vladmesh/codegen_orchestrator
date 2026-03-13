# Admin auth + single entry point — proxy Grafana through admin, close extra ports

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 1.5 of the admin panel — make admin-frontend the single entry point.
Currently Grafana (:3000) and API (:8000) are exposed without auth. Logs open in a new tab.
After this task: only port 3001 exposed, protected by basic auth, Grafana embedded inline.

Brainstorm: docs/brainstorms/admin-panel.md (Phase 1.5 section)

Current state:
- nginx.conf proxies /api/* and /debug/* — no auth
- Grafana has GF_SECURITY_ALLOW_EMBEDDING=true but NO sub-path config
- Sidebar uses externalUrl() to open Grafana in new tab on port 3000
- docker-compose exposes ports 3001, 8000, 3000

## Steps

1. [ ] Nginx basic auth — htpasswd + nginx config
   - **Input**: services/admin-frontend/nginx.conf, docker-compose.yml
   - **Output**:
     - Generate htpasswd file: `services/admin-frontend/htpasswd` (gitignored, generated at build time or mounted)
     - Add ADMIN_PASSWORD env var to .env.example and docker-compose
     - Dockerfile: `RUN apk add --no-cache apache2-utils` + generate htpasswd at build or use entrypoint script
     - nginx.conf: add `auth_basic` + `auth_basic_user_file` to server block
     - Exclude /health from auth (for docker healthcheck)
   - **Test**: `docker compose up admin-frontend` → curl without creds returns 401, curl with creds returns 200

2. [ ] Grafana sub-path configuration
   - **Input**: docker-compose.yml (grafana service environment)
   - **Output**:
     - Add `GF_SERVER_ROOT_URL: "%(protocol)s://%(domain)s:%(http_port)s/grafana/"` 
     - Add `GF_SERVER_SERVE_FROM_SUB_PATH: "true"`
   - **Test**: Grafana responds correctly when accessed via /grafana/ path

3. [ ] Nginx proxy for Grafana
   - **Input**: services/admin-frontend/nginx.conf
   - **Output**:
     - Add `location /grafana/ { proxy_pass http://grafana:3000/grafana/; }` with proper proxy headers
     - Add grafana to admin-frontend depends_on in docker-compose
   - **Test**: curl localhost:3001/grafana/ (with auth) returns Grafana UI

4. [ ] Close external ports in docker-compose
   - **Input**: docker-compose.yml
   - **Output**:
     - Remove `ports: - "3000:3000"` from grafana service (keep expose or nothing — internal network suffices)
     - Remove `ports: - "8000:8000"` from api service
     - Only admin-frontend keeps `ports: - "3001:80"`
   - **Test**: `curl localhost:3000` fails (connection refused), `curl localhost:8000` fails, `curl -u admin:pass localhost:3001/api/health` works

5. [ ] Frontend — Logs page as embedded iframe
   - **Input**: services/admin-frontend/src/components/layout/Sidebar.tsx, new LogsPage component
   - **Output**:
     - Change Logs nav item from external link to internal route `/logs`
     - Create LogsPage.tsx — full-height iframe pointing to `/grafana/d/service-logs/service-logs?orgId=1&kiosk`
     - Add route in App.tsx
     - Remove externalUrl helper if no longer used (LLM Tracing is disabled)
   - **Test**: Navigate to /logs → Grafana dashboard renders in iframe within the admin layout

6. [ ] Entrypoint script for htpasswd generation
   - **Input**: services/admin-frontend/Dockerfile
   - **Output**:
     - Create entrypoint.sh that generates htpasswd from ADMIN_USER + ADMIN_PASSWORD env vars at container start
     - Update Dockerfile to use entrypoint.sh (so password is not baked into image)
     - docker-compose: pass ADMIN_USER and ADMIN_PASSWORD env vars
   - **Test**: Change ADMIN_PASSWORD in .env → restart → old password rejected, new password works


