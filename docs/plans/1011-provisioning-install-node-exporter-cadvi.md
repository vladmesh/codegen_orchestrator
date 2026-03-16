# #1011 Provisioning: install node_exporter + cadvisor + UFW rules

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

This is Phase 1 task from the server health monitoring brainstorm (bs-69482380). Currently, the orchestrator has no real visibility into server health — metrics come only from the Time4VPS provider API which gives inaccurate RAM data and no CPU usage.

**Current state:**
- A `monitoring` Ansible role already exists at `services/infra-service/ansible/roles/monitoring/` with node_exporter in Docker, but it is **not included** in `provision_software.yml`
- The role is incomplete: no cadvisor, no UFW rules, no defaults file
- UFW currently allows SSH (22), HTTP (80), HTTPS (443) globally — no IP-restricted rules
- The orchestrator has no `ORCHESTRATOR_IP` env var — needed for UFW allow rules
- `monitoring_enabled: true` and `node_exporter_port: 9100` already defined in `all.yml`

**Sibling tasks** (all backlog): #1012 (parser), #1013 (DB model), #1014 (health_checker worker), #1015-#1019 (UI, probes, drift)

## Steps

1. [ ] Add `ORCHESTRATOR_PUBLIC_IP` env var to infra-service
   - **Input**: `docker-compose.yml`, `.env.example`, `services/infra-service/src/provisioner/node.py`
   - **Output**: New env var `ORCHESTRATOR_PUBLIC_IP` available to infra-service container, passed to Ansible as `orchestrator_ip` extra var. Add to `.env.example` with comment. Fail fast if not set when running provisioning.
   - **Test**: Unit test that `AnsibleRunner.run_playbook()` includes `orchestrator_ip` in extra_vars when the env var is set

2. [ ] Extend monitoring role: add cadvisor, add defaults
   - **Input**: `services/infra-service/ansible/roles/monitoring/`
   - **Output**:
     - `defaults/main.yml` with `monitoring_enabled`, `node_exporter_port: 9100`, `cadvisor_port: 8080`, `orchestrator_ip` (required)
     - `tasks/main.yml` updated: node_exporter stays as Docker container (already works), add cadvisor as Docker container (image: `gcr.io/cadvisor/cadvisor:latest`, volumes: `/:/rootfs:ro`, `/var/run:/var/run:ro`, `/sys:/sys:ro`, `/var/lib/docker:/var/lib/docker:ro`, port 8080)
     - Both containers use `restart: unless-stopped`
   - **Test**: Unit test (YAML structure + validity, similar to `test_ansible_qa_runner_role.py`): role has defaults, tasks are valid YAML, cadvisor and node_exporter both present, all tasks have names

3. [ ] Add UFW rules for monitoring ports (orchestrator IP only)
   - **Input**: `services/infra-service/ansible/roles/monitoring/tasks/main.yml`
   - **Output**: UFW tasks added to monitoring role: `ufw allow from {{ orchestrator_ip }} to any port {{ node_exporter_port }} proto tcp` and same for cadvisor_port. Also `ufw deny` rules for those ports from other sources (explicit deny before the compose starts).
   - **Test**: Unit test: YAML contains UFW tasks with correct port variables and `from` restriction

4. [ ] Include monitoring role in `provision_software.yml`
   - **Input**: `services/infra-service/ansible/playbooks/provision_software.yml`
   - **Output**: Add monitoring section after Docker installation (section 3) and before System Configuration (section 5). Use `include_role: name=monitoring` with tag `[monitoring]`. This ensures Docker is available before monitoring containers start.
   - **Test**: Unit test: `provision_software.yml` is valid YAML, contains monitoring include after Docker section

5. [ ] Pass `orchestrator_ip` through AnsibleRunner to playbooks
   - **Input**: `services/infra-service/src/provisioner/ansible_runner.py`, `services/infra-service/src/provisioner/node.py`
   - **Output**: `AnsibleRunner.run_playbook()` accepts optional `orchestrator_ip` parameter, appends `orchestrator_ip=<value>` to extra_vars. `ProvisionerNode` reads `ORCHESTRATOR_PUBLIC_IP` from env and passes it to all `run_playbook()` calls.
   - **Test**: Unit test: mock subprocess, verify `orchestrator_ip` appears in ansible-playbook command args

6. [ ] Integration test: verify Ansible playbook structure end-to-end
   - **Input**: All modified files from steps 1-5
   - **Output**: Integration test that loads `provision_software.yml`, resolves role includes, and validates the full provisioning chain includes monitoring with correct variable references. Verify `group_vars/all.yml` variables align with role defaults.
   - **Test**: This IS the integration test step

