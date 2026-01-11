# Service: Infra Service

**Service Name:** `infra-service`
**Current Name:** `infra-service` (formerly `infrastructure-worker`)
**Responsibility:** Execution of low-level infrastructure operations (Ansible, Provisioning, Deployment).

## 1. Responsibilities

The `infra-service` is the "Hands" of the DevOps Subgraph. It isolates heavy dependencies (Ansible, SSH, cryptography) and executes potentially dangerous operations.

1.  **Server Provisioning**: preparing a fresh VPS (User/Setup management, Docker install, Security hardening).
2.  **Application Deployment**: deploying the Docker Compose stack of a Project to a target Server.
3.  **Incident Recovery**: auto-fixing common server issues (disk space, docker hang).
4.  **Credential Management**: usage of SSH keys (secrets) without exposing them to the Orchestrator/LLM.

## 2. API (Redis Queues)

The service acts as a consumer for two main queues.

### 2.1 Provisioning (`provisioner:queue`)

**Producer:** `Scheduler` (periodic checks).

**Message Payload (`ProvisionerMessage`):**
*   `server_handle` (string): The identifier of the server (e.g., in Time4VPS or internal DB).
*   `force_reinstall` (bool): If True, triggers full `make nuke` equivalent / OS rebuild.
*   `is_recovery` (bool): If True, runs specific recovery playbooks instead of full setup.

**Workflow:**
1.  Fetch Server details (IP, login credentials) from internal Source of Truth (Database/Time4VPS API).
2.  Check SSH connectivity.
3.  Run `ansible-playbook setup_server.yml`.
4.  Report result to `provisioner:results`.

### 2.2 Deployment (`ansible:deploy:queue`)

**Producer:** `LangGraph` (DevOps Subgraph -> DeployerNode).

**Message Payload (`AnsibleDeployMessage`):**
*   `project_id` (string): UUID.
*   `repo_full_name` (string): "org/repo".
*   `server_ip` (string): Target server address.
*   `port` (int): Allocated service port.
*   `modules` (list): Enabled modules (to enable/disable docker-compose profiles).
*   `github_token_ref` (string): Key to fetch GitHub Token from Vault/Secrets.
*   `secrets_ref` (string): Key to fetch Project Secrets (`.env` content).

**Workflow:**
1.  **Resolving Secrets**: Fetch the actual sensitive values (GH Token, Env Vars) using the provided references. *Safety check: Ensure we don't log these.*
2.  **Prepare Inventory**: Generate a temporary in-memory Ansible inventory for the specific host.
3.  **Run Playbook**: Execute `deploy_project.yml`.
    *   Variables: `repo`, `branch`, `port`, `env_vars`.
4.  **Result**: Publish `AnsibleDeployResult` to `deploy:results` (Shared Queue).

## 3. Architecture & Isolation

### 3.1 Why a separate service?
*   **Dependencies**: Ansible requires Python with system packages, SSH agents, and complex dependencies that shouldn't pollute the `langgraph` or `api` images.
*   **Security**: This service handles raw SSH private keys and root access to servers. It should be strictly isolated.
*   **Concurrency**: Ansible plays are blocking and IO-bound. We can scale `infra-service` instances horizontally to handle multiple parallel deployments.

### 3.2 Ansible Structure
The service wraps the `ansible/` directory.

*   `playbooks/setup_server.yml`: Base provisioning (User, Docker, Firewall).
*   `playbooks/deploy_project.yml`: Project deployment (Git pull, Docker build/up).
*   `roles/`: Reusable logic.

## 4. Error Handling

*   **SSH Failures**: 
    *   Retry 3 times with exponential backoff.
    *   If unreachable, mark Server as `UNREACHABLE` in DB.
*   **Playbook Failures**: 
    *   Capture `stderr` from `ansible-runner`.
    *   Return structured error to the caller.
    *   *Do not* automatically retry heavy playbook runs (idempotency is good, but infinite loops are bad).

## 5. Dependencies

*   `ansible-core`
*   `ansible-runner` (Python API for Ansible)
*   `asyncpg` (DB access for updating Server status)
*   `redis`
*   `requests` (Time4VPS API)

## 6. Implementation Details & Caveats

### 6.1 Concurrency (Blocking Calls)
*   **Current Issue**: `ansible_runner` currently uses blocking `subprocess.run`, which freezes the worker loop.
*   **Requirement**: Migration to `asyncio.create_subprocess_exec` is required to allow concurrency within a single worker instance.
*   **Workaround**: For MVP, we can rely on horizontal scaling (multiple worker containers) since Redis Groups handle load balancing efficiently.

### 6.2 Secret Handling
*   **Safety**: Never pass secrets (GH Tokens, Passwords) as command-line arguments to Ansible (`--extra-vars "token=..."`). This leaks into process lists and logs.
*   **Mechanism**: Use **Environment Variables**. Ansible automatically maps env vars to variables if configured, or they can be accessed via `lookup('env', 'MY_SECRET')`.
*   **Flow**:
    ```python
    # Secure execution
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = resolved_token
    await asyncio.create_subprocess_exec(..., env=env)
    ```
