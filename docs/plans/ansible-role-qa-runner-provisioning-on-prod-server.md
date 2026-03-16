# Ansible role: qa_runner provisioning on prod servers

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Post-deploy QA uses Claude Code CLI on prod servers (see brainstorm bs-eece61a8). The QA consumer (task-22130356, done) and deploy→QA wiring are merged. The consumer SSHes to the server and runs `claude` CLI — but Claude Code, Python QA deps, and the `/opt/qa-runner/` directory are not yet installed on servers.

Current Ansible structure: `provision_software.yml` is an inline playbook (no roles), while `site.yml` uses roles. The `qa_runner` role should be added to `site.yml` and optionally runnable standalone via `provision_software.yml` or a dedicated tag.

The QA runner code (`_qa_runner.py`) expects:
- Claude Code CLI available as `claude` command
- `/opt/services/{project}` directory (already exists from deploy)
- Telethon session at `/opt/qa-runner/telethon.session` (optional)
- `ANTHROPIC_API_KEY` available to the `root` user (SSH connects as root)

## Steps

1. [ ] Create Ansible role directory structure
   - **Input**: `services/infra-service/ansible/roles/qa_runner/`
   - **Output**: `tasks/main.yml`, `defaults/main.yml` with the full role
   - **Test**: `ansible-playbook --syntax-check` passes (unit test: validate YAML structure)

   Sub-tasks:
   - `defaults/main.yml`: define `qa_runner_dir: /opt/qa-runner`, `deploy_user: deploy`, `nodejs_major_version: 22`
   - `tasks/main.yml`:
     a. Create `/opt/qa-runner/` directory (owned by root — QA SSH runs as root)
     b. Install Node.js via NodeSource APT repo (not the outdated Ubuntu `nodejs` package)
     c. Install `@anthropic-ai/claude-code` globally via npm
     d. Install Python packages: `telethon`, `httpx` via pip3
     e. Write ANTHROPIC_API_KEY to `/opt/qa-runner/.env` (from `anthropic_api_key` var)
     f. Copy Telethon session file conditionally (`when: telethon_session_file is defined`)
   - All tasks must be idempotent (`state: present`, conditional copies)

2. [ ] Add `anthropic_api_key` to Ansible vault in `group_vars/all.yml`
   - **Input**: `services/infra-service/ansible/group_vars/all.yml`
   - **Output**: Vault-encrypted `anthropic_api_key` variable (placeholder — actual value set at deploy time)
   - **Test**: Variable is defined in the file (manual verification — vault values can't be unit tested)
   - Note: For now, add a commented placeholder. The actual vault value will be set manually via `ansible-vault encrypt_string`.

3. [ ] Include `qa_runner` role in `site.yml`
   - **Input**: `services/infra-service/ansible/playbooks/site.yml`
   - **Output**: Role added after `secrets` and before `services` with tag `[qa]`
   - **Test**: `ansible-playbook --syntax-check site.yml` passes

4. [ ] Add QA runner section to `provision_software.yml`
   - **Input**: `services/infra-service/ansible/playbooks/provision_software.yml`
   - **Output**: New section "7. QA Runner" that includes the `qa_runner` role with tag `[qa]`
   - **Test**: `ansible-playbook --syntax-check provision_software.yml` passes

5. [ ] Unit tests for role YAML validity
   - **Input**: New test file `services/infra-service/tests/unit/test_ansible_roles.py`
   - **Output**: Tests that validate:
     - Role directory structure exists (tasks/main.yml, defaults/main.yml)
     - YAML is valid and parseable
     - All tasks have `name` field
     - Key tasks are present (Node.js install, Claude Code install, dir creation)
   - **Test**: `pytest services/infra-service/tests/unit/test_ansible_roles.py`

