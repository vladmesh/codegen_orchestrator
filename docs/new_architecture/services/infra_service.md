# Service: Infra Service

**Service Name:** `infra-service`
**Current Name:** `infra-service` (formerly `infrastructure-worker`)
**Responsibility:** "Bare metal" server provisioning and maintenance.

## 1. Responsibilities

The `infra-service` prepares the infrastructure layer. It does NOT deploy applications.

1.  **Server Provisioning**: preparing a fresh VPS (User/Setup management, Docker install, Security hardening).
2.  **Incident Recovery**: auto-fixing common server issues (disk space, docker hang).
3.  **Deploy Key Setup**: installing the Orchestrator's SSH public key to allow GitHub Actions to access the server.

> **Note:** Application deployment is handled explicitly by GitHub Actions (see DEPLOYMENT_STRATEGY.md).

## 2. API (Redis Queues)

The service acts as a consumer for a single queue.

### 2.1 Provisioning (`provisioner:queue`)

**Producer:** `Scheduler` (periodic checks).

**Message Payload (`ProvisionerMessage`):**
*   `server_handle` (string): The identifier of the server (e.g., in Time4VPS or internal DB).
*   `force_reinstall` (bool): If True, triggers full `make nuke` equivalent / OS rebuild.
*   `is_recovery` (bool): If True, runs specific recovery playbooks instead of full setup.

**Workflow:**
1.  Fetch Server details (IP, login credentials) from internal Source of Truth.
2.  Check SSH connectivity.
3.  **Deploy Key Setup**:
    *   Read `ORCHESTRATOR_SSH_PUBLIC_KEY` from service environment.
    *   Add it to `/root/.ssh/authorized_keys` (or appropriate user) on the target server.
    *   This ensures future GitHub Actions can SSH in using the corresponding private key.
4.  Run `ansible-playbook setup_server.yml` (Docker, firewall, users).
5.  Report result to `provisioner:results`.

## 3. Architecture & Isolation

### 3.1 Why a separate service?
*   **Dependencies**: Ansible requires Python with system packages and SSH agents.
*   **Security**: This service handles raw SSH access to servers. It should be strictly isolated.
*   **Concurrency**: Ansible plays are blocking and IO-bound.

### 3.2 Ansible Structure
The service wraps the `ansible/` directory.

*   `playbooks/setup_server.yml`: Base provisioning (User, Docker, Firewall).
*   `playbooks/provision_access.yml`: specialized playbook for SSH key management (optional, or part of setup).
*   `roles/`: Reusable logic.

> **Removed:** `deploy_project.yml` is no longer used.

## 4. Error Handling

*   **SSH Failures**: 
    *   Retry 3 times with exponential backoff.
    *   If unreachable, mark Server as `UNREACHABLE` in DB.
*   **Playbook Failures**: 
    *   Capture `stderr` from `ansible-runner`.
    *   Return structured error to the caller (Scheduler).

## 5. Dependencies

*   `ansible-core`
*   `ansible-runner` (Python API for Ansible)
*   `asyncpg` (DB access for updating Server status)
*   `redis`
*   `requests` (Time4VPS API)

## 6. Implementation Details & Caveats

### 6.1 Concurrency
*   **Strategy**: Horizontal scaling (multiple worker containers).
*   **Future**: Migration to `asyncio` subprocess execution.

### 6.2 Secret Handling
*   **Provisioning Secrets**: Uses L1 secrets (Provider API keys, Root passwords).
*   **Mechanism**: Environment variables only. No CLI arguments.
