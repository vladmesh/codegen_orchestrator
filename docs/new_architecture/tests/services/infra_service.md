# Testing Strategy: Infra Service

This document defines the testing strategy for the `infra-service`.
Due to the nature of this service (interacting with remote servers via SSH, running heavy Ansible playbooks), the testing strategy heavily relies on **execution abstraction** and **mocking**.

## 1. Philosophy: Mock Executable

We follow the principle: **"Execute logic, mock the heavy lifting."**

The `infra-service` is essentially a wrapper around `ansible-playbook` and `ssh` commands.
We do NOT want to provision real servers or deploy real apps in our CI/Integration tests.

*   **Logic (Unit)**: Verify that the service constructs the correct Ansible arguments, inventory files, and configuration based on inputs.
*   **Execution (Integration)**: Run the service with a "Mock Runner" that intercepts subprocess calls and returns predefined outcomes (Success, Unreachable, Failed).
*   **End-to-End (Nightly)**: Only here do we touch real SSH endpoints (using a dedicated long-lived test server or a containerized `sshd`).

## 2. Test Pyramid

| Level | Scope | Focus | Implementation |
|-------|-------|-------|----------------|
| **Unit** | `AnsibleParams`, `InventoryBuilder` | Argument construction, File generation | Pure Python, File system mocks |
| **Integration** | `InfraService` | Redis I/O + Subprocess Mocking | Full flow, Error handling, State updates |
| **E2E (Nightly)** | Full Pipeline | Real SSH + Real Ansible | Connectivity, Playbook syntax verification |

## 3. Test Scenarios

### 3.1 Unit Tests (Pure Logic)

These tests ensure we don't send garbage to Ansible.

**Key Scenarios:**
*   **Inventory Generation:**
    *   Input: `AnsibleDeployMessage(server_ip="1.2.3.4", port=8000, ...)`
    *   Assert: `inventory.ini` generated with correct structure (`[webservers]`, `ansible_host=1.2.3.4`, `ansible_user=root`).
*   **Secret Injection:**
    *   Input: `secrets_ref="proj-123-env"`
    *   Assert: `runner` receives secrets via `env` variables (e.g., `GITHUB_TOKEN`, `PROJECT_SECRETS`), NOT command line args.
*   **Playbook Selection:**
    *   Input: `ProvisionerMessage(is_recovery=True)`
    *   Assert: Selects `playbooks/recovery.yml`.

### 3.2 Integration Tests (The Core)

These tests run the full service loop but bypass the actual `subprocess.run`.

**Mocks:**
*   **Subprocess Mock:** A configurable fixture that matches commands (regex) and returns `(return_code, stdout, stderr)`.
*   **Redis:** Real Redis instance.

**Scenario A: Successful Server Provisioning**
1.  **Trigger:** Publish `ProvisionerMessage(server_handle="srv-1", force_reinstall=False)` to `provisioner:queue`.
2.  **Mock Behavior:** Subprocess Interceptor sees `ansible-playbook setup_server.yml` and returns `rc=0`, `stdout="{...result json...}"`.
3.  **Assert:** 
    *   Service continues running.
    *   `ProvisionerResult` published to `provisioner:results`.
    *   Status is `success`.

**Scenario B: Server Unreachable (SSH Error)**
1.  **Trigger:** Publish `ProvisionerMessage(...)`.
2.  **Mock Behavior:** Subprocess Interceptor returns `rc=4` (Ansible unreachable code) and `stderr="SSH connection failed"`.
3.  **Assert:**
    *   Service reports `ProvisionerResult(status="failed", error="SSH connection failed")`.
    *   Service does *not* crash.

**Scenario C: Deployment Success**
1.  **Trigger:** Publish `AnsibleDeployMessage(project_id="p1", ...)` to `ansible:deploy:queue`.
2.  **Mock Behavior:** Interceptor returns `rc=0`.
3.  **Assert:** `AnsibleDeployResult` published to `deploy:results`.

### 3.3 E2E Tests (Nightly Only)

**Goal:** Verify that our Playbooks are syntactically valid and actually work on a real OS.

**Infrastructure:**
*   **Target:** A Docker container running `sshd` (simulating a clean Ubuntu server).
*   **Keys:** Pre-generated SSH keys injected into the Test Runner and Target.

**Scenario:**
1.  Spin up `target-server` container (sshd + python3).
2.  Send `ProvisionerMessage(server_ip="<target_ip>")` to `infra-service`.
3.  Wait for `ProvisionerResult(status="success")`.
4.  **Verification:** Connect to `target-server` and check if expected packages (e.g., Docker) are installed (via `ssh user@ip "docker --version"`).

## 4. Test Infrastructure & Fixtures

### 4.1 `MockRunner` Fixture

```python
class MockAnsibleRunner:
    def __init__(self):
        self.calls = []
        self.scenarios = [] # List of (regex, outcome)

    def add_case(self, cmd_pattern: str, rc: int, stdout: str = "", stderr: str = ""):
        self.scenarios.append((re.compile(cmd_pattern), rc, stdout, stderr))

    async def run(self, cmd: list[str], env: dict):
        self.calls.append({"cmd": cmd, "env": env})
        cmd_str = " ".join(cmd)
        
        for pattern, rc, out, err in self.scenarios:
            if pattern.search(cmd_str):
                return ExecutionResult(rc, out, err)
        
        return ExecutionResult(0, "{}", "") # Default success
```

### 4.2 `TempWorkspace`
A fixture that creates a temporary directory for generating inventory files and cleans it up after the test.

## 5. Implementation Plan

1.  **Refactor**: Ensure `infra-service` uses a dependency-injected `Runner` class instead of calling `subprocess` directly. This allows swapping Real/Mock runners.
2.  **Create Harness**: Implement the `InfraTestHarness` similar to `LangGraphTestHarness` (manages Redis + Runner).
3.  **Write Integration Tests**: Cover the 3 main scenarios (Provision, Deploy, Fail).
