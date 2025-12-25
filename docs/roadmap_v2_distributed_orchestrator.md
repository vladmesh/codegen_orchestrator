# 🗺️ Roadmap v2: Distributed Orchestrator & Source of Truth

**Goal**: Transform the system into a robust, autonomous multi-agent orchestrator with a hierarchical topology and a strict "Source of Truth" philosophy.

---

## 🏗 Phase 1: Foundation & Source of Truth [DONE]
**Objective**: Establish the "Heartbeat" of the system. Ensure the database always reflects reality (GitHub).

### 1.1 Enhanced Data Models [DONE]
*   **Project Model**:
    *   Add `github_repo_id` (int) for immutable tracking.
    *   Update `status` to use the new Lifecycle Enum:
        *   `draft`: Inception/discussion.
        *   `estimated`: Spec generated.
        *   `provisioning`: Repo creation in progress.
        *   `initialized`: Repo exists (Source of Truth established).
        *   `designing` / `designed`: Architect work.
        *   `implementing` / `implemented`: Developer work.
        *   `verifying` / `verified`: Tester work.
        *   `deploying` / `active`: Live on prod.
        *   `maintenance`: Updating active project.
        *   `missing`: Incident (Repo gone).
*   **Server Model**:
    *   Ensure `ServerStatus` covers all lifecycle stages (`discovered`, `provisioning`, `ready`, `error`).

### 1.3 Secrets Management (Security) [PARTIAL]
*   **Goal**: Securely store environment variables required for deployment.
*   **Approach**:
    *   Store encrypted `.env` templates in the Database (or use a simple Vault solution).
    *   Ensure "Discovered" projects are flagged as `setup_required` if secrets are missing.

### 1.4 Polling Consolidation (Scheduler Service) [DONE]
*   **Goal**: Move all background polling/monitoring from API to a dedicated service.
*   **Rationale**: API should be a clean CRUD layer. Polling workers need SSH keys and other credentials that shouldn't be in API.
*   **Implementation**:
    *   New `scheduler` service in Docker Compose.
    *   Contains: `github_sync`, `server_sync`, `health_checker`, `provisioner_trigger` workers.
    *   Shares database and Redis with API.
    *   Has access to SSH keys for health checks.

### 1.2 GitHub Polling Service (The Heartbeat) [DONE]
*   **Worker**: "Sync Projects Worker" (separate from LangGraph).
*   **Frequency**: Every 5-10 minutes.
*   **Logic**:
    *   Fetch all repos from Org.
    *   **New**: Create `Project` in DB with status `discovered`.
    *   **Existing**: Update name/metadata if changed.
    *   **Missing**:
        *   If `404 Not Found` -> Mark as `missing` (Critical Alert).
        *   If `Network Error / 5xx` -> Ignore (Transient failure), increment fail_count. Only alert after N consecutive failures.

---

## 🧠 Phase 2: The Strategic Level (Product Owner) [DONE]
**Objective**: Implement the central brain that manages high-level logic and user communication.

### 2.1 Product Owner (PO) Agent [DONE]
*   **Role**: Strategic decisions, User Interface, Dispatcher.
*   **Tools**:
    *   `list_projects`: Summary of all projects.
    *   `get_project_status`: Detailed state.
    *   `create_project_intent`: Start the "Brainstorm" phase.
*   **Logic**:
    *   Classify user intent (New / Status / Update).
    *   Manage the `OrchestratorState` to track sub-agent progress.

### 2.2 Parallel Dispatch [DONE]
*   **Mechanism**: Use LangGraph `Send` or parallel branches.
*   **Flow**:
    *   User: "New Project".
    *   PO: Triggers **Zavhoz** (Resource Alloc) AND **Architect** (Design) simultaneously.

---

## 🏗️ Phase 3: The Graph Hierarchy (Re-Architecture) [DONE]
**Objective**: Move from specific linear flows to a delegated hierarchy using the Star/Tree topology.

### 3.1 Graph Refactoring & Subgraphs [DONE]
*   **Concept**: Use **LangGraph Subgraphs** to encapsulate complexity.
*   **Engineering Subgraph**:
    *   Combines `Architect`, `Developer`, and `Tester`.
    *   Exposes a single node to the PO: "Engineering".
    *   Internally loops until verified (max 3 iterations).
*   **Topology**:
    *   **Level 1**: `User <-> PO`.
    *   **Level 2**:
        *   `PO -> Zavhoz` (Resources).
        *   `PO -> Engineering Subgraph` (Creation & Logic).
    *   **Level 3 (Inside Engineering)**:
        *   `Architect -> Developer` (Code Loop).
        *   `Tester` (Verification).

### 3.2 State Management [DONE]
*   **Sub-Graph Tracking**:
    *   `engineering_status`: `idle` | `working` | `done` | `blocked`.
    *   `needs_human_approval`: boolean for Human-in-the-Loop.
    *   `engineering_iterations`: counter for loop limit.

---

## ⚙️ Phase 4: Engineering & Operations Integration [DONE]
**Objective**: Connect the specialists loop and deployment.

### 4.1 Architect's "Sub-Graph" & Human-in-the-Loop [DONE]
*   **Orchestration**: Architect loops with Developer inside Engineering Subgraph.
*   **Human-in-the-Loop (Max Iterations)**:
    *   If iterations >= 3 and tests still fail:
        *   Set `needs_human_approval = True`.
        *   Route to END (wait for user).
        *   User can provide feedback or manual intervention.

### 4.2 Deployment Pipeline [DONE]
*   **Trigger**: Engineering Subgraph sets `engineering_status = "done"`.
*   **Pre-flight Check**: Check `allocated_resources` (from Zavhoz).
    *   ✅ Ready: Call **DevOpsNode**.
    *   ❌ Not Ready: Return to END (wait for resources).

### 4.3 DevOps Node [DONE]
*   **Input**: `repo_info` + `allocated_resources`.
*   **Action**: Run Ansible playbook.
*   **Output**: Update Project Status to `active`.

---

## 🛡️ Phase 5: Resilience & Maintenance
**Objective**: Handle edge cases and long-running lifecycles.

### 5.1 Project Maintenance
*   **Scenario**: User asks to update an active project.
*   **Flow**:
    *   PO sets status `maintenance`.
    *   Dispatches Architect (Refactor/Feature).
    *   Standard Dev Loop.
    *   Deploy (Blue/Green if possible, otherwise downtime).

### 5.2 Incident Management
*   **Scenario**: GitHub Sync detects missing repo / Server Sync detects down server.
*   **Flow**:
    *   Mark as `error` / `missing`.
    *   PO alerts User on next interaction.
    *   User requests "Fix" -> PO dispatches Recovery Agent (DevOps/Zavhoz).

---

## 📅 Execution Order

1.  **DB Migration**: Add Project Statuses & GitHub ID.
2.  **Sync Service**: Implement GitHub Polling.
3.  **PO Agent**: Basic implementation (Echo + Intent).
4.  **Graph Topology**: rewire to `PO <-> {Zavhoz, Architect}`.
5.  **Architect Logic**: Implement Dev/Test loop orchestration.
6.  **Integration**: Full E2E flow (New Project -> PO -> Parallel -> Ready).
