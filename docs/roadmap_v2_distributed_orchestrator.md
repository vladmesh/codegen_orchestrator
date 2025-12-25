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

## 🧠 Phase 2: The Strategic Level (Product Owner)
**Objective**: Implement the central brain that manages high-level logic and user communication.

### 2.1 Product Owner (PO) Agent
*   **Role**: Strategic decisions, User Interface, Dispatcher.
*   **Tools**:
    *   `list_projects`: Summary of all projects.
    *   `get_project_status`: Detailed state.
    *   `create_project_intent`: Start the "Brainstorm" phase.
*   **Logic**:
    *   Classify user intent (New / Status / Update).
    *   Manage the `OrchestratorState` to track sub-agent progress.

### 2.2 Parallel Dispatch
*   **Mechanism**: Use LangGraph `Send` or parallel branches.
*   **Flow**:
    *   User: "New Project".
    *   PO: Triggers **Zavhoz** (Resource Alloc) AND **Architect** (Design) simultaneously.

---

## 🏗️ Phase 3: The Graph Hierarchy (Re-Architecture)
**Objective**: Move from specific linear flows to a delegated hierarchy using the Star/Tree topology.

### 3.1 Graph Refactoring & Subgraphs
*   **Concept**: Use **LangGraph Subgraphs** to encapsulate complexity.
*   **Engineering Subgraph**:
    *   Combines `Architect`, `Developer`, and `Tester`.
    *   Exposes a single node to the PO: "Develop Feature X".
    *   Internally loops until verified.
*   **Topology**:
    *   **Level 1**: `User <-> PO`.
    *   **Level 2**:
        *   `PO -> Zavhoz` (Resources).
        *   `PO -> Engineering Subgraph` (Creation & Logic).
    *   **Level 3 (Inside Engineering)**:
        *   `Architect <-> Developer` (Code Loop).
        *   `Tester` (Verification).

### 3.2 State Management
*   **Sub-Graph Tracking**:
    *   `engineering_status`: `idle` | `working` | `blocked`.
    *   `zavhoz_status`: `idle` | `working` | `done`.

---

## ⚙️ Phase 4: Engineering & Operations Integration
**Objective**: Connect the specialists loop and deployment.

### 4.1 Architect's "Sub-Graph" & Human-in-the-Loop
*   **Orchestration**: Architect naturally loops with Developer.
    *   "Write code" -> Dev -> "Done" -> Arch -> "Review".
*   **Human-in-the-Loop (Interruption)**:
    *   Use LangGraph `interrupt` mechanism.
    *   If Architect/Dev is stuck or needs confirmation:
        *   Pause execution.
        *   Notify User (via PO).
        *   Wait for User Input (e.g., feedback or manual file edit).
        *   Resume execution from the interruption point.

### 4.2 Deployment Pipeline
*   **Trigger**: Architect/Tester confirms `verified` status.
*   **Pre-flight Check**: Check `server_info` (from Zavhoz).
    *   ✅ Ready: Call **DevOpsNode**.
    *   ❌ Not Ready: Return `waiting_for_resources` to PO.

### 4.3 DevOps Node
*   **Input**: `repo_info` + `server_info`.
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
