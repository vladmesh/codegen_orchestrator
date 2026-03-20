# #1031 Promtail on prod servers + expose Loki

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Task #1031 from the User Dashboard story. The orchestrator already runs Loki + Promtail in docker-compose for local/dev logs. Production servers have a monitoring Ansible role with node-exporter and cAdvisor but no log shipping. This task adds:

1. Promtail to the monitoring Ansible role — ships Docker container logs from prod servers to the orchestrator Loki.
2. Loki exposure via Caddy with Basic Auth — so prod Promtail instances can push over HTTPS.

Current state:
- `infra/loki.yml` — Loki config, `auth_enabled: false`, internal Docker network only
- `infra/promtail.yml` — dev Promtail, scrapes orchestrator containers via docker socket
- `services/infra-service/ansible/roles/monitoring/` — deploys node-exporter + cAdvisor, has UFW rules scoped to `orchestrator_ip`
- `infra/Caddyfile` — reverse proxies registry only
- Caddy on orchestrator runs on `internal` Docker network, ports 80/443 exposed

Key decision: expose Loki through Caddy on the orchestrator hostname (e.g. `https://orch.example.com/loki/*`) with Basic Auth. This avoids opening port 3100 and reuses existing TLS. Prod Promtail pushes to `https://ORCHESTRATOR_HOSTNAME/loki/loki/api/v1/push`.

## Steps

1. [ ] Add Loki route to orchestrator Caddyfile
   - **Input**: `infra/Caddyfile`
   - **Output**: New `handle /loki/*` block with `basic_auth` (using `LOKI_PUSH_USER` / `LOKI_PUSH_PASSWORD_HASH` env vars) that strips prefix and proxies to `loki:3100`. Caddy already shares the `internal` network with Loki — no port exposure needed.
   - **Test**: `make up`, then `curl -u user:pass https://localhost/loki/ready` returns 200 (or via docker exec into caddy network)

2. [ ] Add Loki push env vars to orchestrator config
   - **Input**: `.env.example`, `docker-compose.yml` (caddy service environment block)
   - **Output**: `LOKI_PUSH_USER` and `LOKI_PUSH_PASSWORD` added to `.env.example` with comments. `LOKI_PUSH_PASSWORD_HASH` (bcrypt, same pattern as `REGISTRY_PASSWORD_HASH`) added. Caddy service gets these three env vars in docker-compose.yml.
   - **Test**: `make up` succeeds, caddy logs show no env errors

3. [ ] Create Promtail config template for prod servers
   - **Input**: `infra/promtail.yml` (reference), task description
   - **Output**: `services/infra-service/ansible/roles/monitoring/templates/promtail.yml.j2` — Jinja2 template. Key differences from dev config: pushes to `{{ loki_push_url }}` with `basic_auth` (username/password from Ansible vars), scrapes containers with label `com.codegen.project_id` (not compose project). Relabel: extract `project_id` from container label `com.codegen.project_id`, extract `service` from `com.docker.compose.service`.
   - **Test**: `ansible-playbook --syntax-check` passes; template renders correctly with test vars

4. [ ] Add Promtail service to monitoring Ansible role
   - **Input**: `services/infra-service/ansible/roles/monitoring/tasks/main.yml`, `defaults/main.yml`
   - **Output**: Monitoring docker-compose gets a `promtail` service (image `grafana/promtail:3.4`, mounts docker socket + containers dir + templated config). Defaults get `loki_push_url`, `loki_push_user`, `loki_push_password` variables. Template is rendered to `/opt/monitoring/promtail.yml` before compose up. No extra UFW rules needed — Promtail pushes outbound over HTTPS (port 443, already allowed).
   - **Test**: `ansible-playbook --syntax-check site.yml` passes; `ansible-lint` clean

5. [ ] Add Loki push credentials to Ansible group vars
   - **Input**: `services/infra-service/ansible/group_vars/all.yml`
   - **Output**: `loki_push_url: "https://{{ orchestrator_hostname }}/loki"`, `loki_push_user`, `loki_push_password` vars. Password should be vault-encrypted in prod.yml (add placeholder in all.yml with comment). Add `orchestrator_hostname` var if not already present.
   - **Test**: Variables resolve correctly in `ansible-playbook --check`

6. [ ] Integration test: verify log flow end-to-end
   - **Input**: All files from steps 1-5
   - **Output**: Manual verification steps documented in task acceptance: deploy monitoring role to a test server, run a container with `com.codegen.project_id=test`, verify logs appear in Grafana on orchestrator. Add a simple smoke test script `infra/scripts/test-loki-push.sh` that curls the Loki push endpoint with a test log entry and verifies it arrives.
   - **Test**: Script exits 0 when Loki push endpoint accepts and stores a test log line

