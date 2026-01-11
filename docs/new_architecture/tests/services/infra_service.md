# Testing Strategy: Infra Service

This document defines the testing strategy for the `infra-service`.
Due to the nature of this service (interacting with remote servers via SSH, running heavy Ansible playbooks), the testing strategy heavily relies on **execution abstraction** and **mocking**.

## 1. Philosophy: Mock Executable

We follow the principle: **"Execute logic, mock the heavy lifting."**

The `infra-service` is essentially a wrapper around `ansible-playbook` and `ssh` commands.
We do NOT want to provision real servers in our CI.

*   **Logic (Unit)**: Verify that the service constructs the correct Ansible arguments and inventory files.
*   **Execution (Integration)**: Run the service with a "Mock Runner" that intercepts subprocess calls.
*   **End-to-End (Nightly)**: Only here do we touch real SSH endpoints.

## 2. Test Pyramid

| Level | Scope | Focus | Implementation |
|-------|-------|-------|----------------|
| **Unit** | `AnsibleParams`, `InventoryBuilder` | Argument construction, File generation | Pure Python, File system mocks |
| **Integration** | `InfraService` | Redis I/O + Subprocess Mocking | Full flow, Error handling, State updates |

## 3. Test Scenarios

### 3.1 Unit Tests (Pure Logic)

These tests ensure we don't send garbage to Ansible.

**Key Scenarios:**
*   **Inventory Generation:**
    *   Input: `ProvisionerMessage(server_handle="srv-1", ...)`
    *   Assert: `inventory.ini` generated with correct structure (`[webservers]`, `ansible_host=...`).
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
2.  **Mock Behavior:** Subprocess Interceptor sees `ansible-playbook setup_server.yml` and returns `rc=0`.
3.  **Assert:** 
    *   Service continues running.
    *   `ProvisionerResult` published to `provisioner:results`.
    *   Status is `success`.

**Scenario B: SSH Key Installation**
1.  **Trigger:** Publish `ProvisionerMessage(...)`
2.  **Mock Behavior:** Subprocess Interceptor captures command.
3.  **Assert:** 
    *   Playbook `provision_access.yml` was called (or logic to add key).
    *   Extra vars include `ssh_public_key`.

**Scenario C: Server Unreachable (SSH Error)**
1.  **Trigger:** Publish `ProvisionerMessage(...)`.
2.  **Mock Behavior:** Subprocess Interceptor returns `rc=4` (Ansible unreachable code).
3.  **Assert:**
    *   Service reports `ProvisionerResult(status="failed", error="SSH connection failed")`.

### 3.3 E2E Tests (Nightly Only)

**Goal:** Verify that our Playbooks are syntactically valid and actually work on a real OS.

**Scenario:**
1.  Spin up `target-server` container (sshd + python3).
2.  Send `ProvisionerMessage(server_ip="<target_ip>")` to `infra-service`.
3.  Wait for `ProvisionerResult(status="success")`.
4.  **Verification:** Connect to `target-server` and check if expected packages/keys are present.

## 4. Implementation Plan

1.  **Refactor**: Ensure `infra-service` uses a dependency-injected `Runner` class.
2.  **Create Harness**: Implement the `InfraTestHarness`.
3.  **Write Integration Tests**: Cover the provisioning scenarios.
