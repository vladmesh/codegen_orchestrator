# #7 Security Audit: Deploy Cleanup

## Context

Task #7 — Security Audit: Deploy Cleanup. Part of the "Security hardening" story.

**Problem**: When user projects are deployed to remote servers via `deploy.yml` (generated from service-template), old Docker images accumulate after each pull+deploy cycle. The generated `deploy.yml.jinja` uses `--remove-orphans` but lacks `docker image prune -f` and container cleanup. Over time this consumes disk space on deployment servers.

**Current state**:
- Orchestrator's own `.github/workflows/deploy.yml` already has a final "Cleanup" step with `docker image prune -f` ✅
- Worker-manager has 4 GC tasks for orchestrator-side containers/images/workspaces ✅
- Generated projects' `deploy.yml.jinja` (in service-template) has NO image cleanup ❌
- Ansible provisioner has NO periodic Docker cleanup cron on remote servers ❌

**What needs to change**:
1. Add post-deploy cleanup to the generated `deploy.yml.jinja` template
2. Add a Docker GC cron job via Ansible for periodic cleanup on all provisioned servers

## Steps

1. [ ] Add cleanup to generated deploy.yml.jinja
   - **Input**: `/home/vlad/projects/service-template/template/.github/workflows/deploy.yml.jinja`
   - **Output**: Add `docker image prune -f` and `docker container prune -f` after the health check section in the deploy SSH script
   - **Test**: Update existing template generation test to verify cleanup commands are present in rendered deploy.yml

2. [ ] Add Ansible role for Docker GC cron
   - **Input**: `services/infra-service/ansible/roles/` (create `docker-gc` role)
   - **Output**: Ansible role that installs a daily cron job: `docker image prune -af --filter "until=72h" && docker container prune -f --filter "until=24h"` on remote servers
   - **Test**: Unit test for role structure (tasks/main.yml exists, cron entry is correct)

3. [ ] Include docker-gc role in provisioning playbook
   - **Input**: `services/infra-service/ansible/playbooks/provision_software.yml`
   - **Output**: Add `docker-gc` role to the software provisioning playbook so all new and existing servers get the cron job
   - **Test**: Verify playbook YAML parses correctly, role is included

4. [ ] Integration test — template renders with cleanup
   - **Input**: service-template copier test suite
   - **Output**: Verify the rendered deploy.yml contains cleanup commands
   - **Test**: Run existing copier tests + new assertion

