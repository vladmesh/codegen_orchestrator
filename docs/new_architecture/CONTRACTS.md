# Contracts / Контракты

Типизированные схемы для REST API и Redis очередей.

## Design Principles

1. **Schema-first** — все сообщения валидируются Pydantic схемами
2. **1:1 Queues** — одна очередь = один Writer → один Consumer (+ optional observers)
3. **Logical Actors** — указываем роль (PO-Worker, langgraph), не техническую прослойку
4. **Traceable** — `correlation_id` для сквозной трассировки

---

## Queue Overview

| Queue | DTO | Initiator | Consumer | Purpose |
|-------|-----|-----------|----------|---------|
| `engineering:queue` | EngineeringMessage | PO-Worker | langgraph | Start development task |
| `deploy:queue` | DeployMessage | PO-Worker | langgraph | Start deploy task |
| `scaffolder:queue` | ScaffolderMessage | langgraph | scaffolder | Init project structure |
| `worker:commands` | WorkerCommand | langgraph, telegram-bot | worker-manager | Spawn/kill workers |
| `worker:responses` | WorkerResponse | worker-manager | langgraph | Worker lifecycle events |
| `worker:lifecycle` | WorkerLifecycleEvent | worker-wrapper | worker-manager | Worker state changes |
| `worker:po:{user_id}:input` | POWorkerInput | telegram-bot | PO worker-wrapper | User message to PO |
| `worker:po:{user_id}:output` | POWorkerOutput | PO worker-wrapper | telegram-bot | PO reply to user |
| `worker:developer:{task_id}:input` | DeveloperWorkerInput | langgraph | Developer worker-wrapper | Task for Developer |
| `worker:developer:{task_id}:output` | DeveloperWorkerOutput | Developer worker-wrapper | langgraph | Developer result |
| `provisioner:queue` | ProvisionerMessage | scheduler | infra-service | Setup server |
| `provisioner:results` | ProvisionerResult | infra-service | scheduler, telegram-bot | Provisioning result |
| `ansible:deploy:queue` | AnsibleDeployMessage | langgraph | infra-service | Run ansible deploy |
| `deploy:result:{request_id}` | AnsibleDeployResult | infra-service | langgraph | Deploy result |

### Transport Layer Note

> **Important:** The "Initiator" column shows the **logical actor** — who makes the decision to publish.
>
> For **Worker-initiated** messages (PO-Worker, Developer-Worker), the actual transport is:
> ```
> Worker (AI Agent) → orchestrator-cli → Redis/API
> ```
> The CLI is a permission-checked proxy, not an independent actor.
> See [cli_orchestrator.md](../packages/cli_orchestrator.md) for details.

### Actor Roles

| Actor | Type | Description |
|-------|------|-------------|
| **PO-Worker** | Worker | Product Owner agent, talks to user |
| **Developer-Worker** | Worker | Developer agent (inside engineering flow) |
| **langgraph** | Service | Workflow orchestrator |
| **worker-manager** | Service | Container lifecycle manager |
| **worker-wrapper** | Process | Agent bridge inside container |
| **telegram-bot** | Service | User interface |
| **scaffolder** | Service | Project initialization |
| **infra-service** | Service | Ansible/provisioning |
| **scheduler** | Service | Background tasks |

### MVP Notes

> [!IMPORTANT]
> **Tester Node** is an MVP stub. It does NOT spawn a Worker.  
> Implementation: A simple LangGraph node that always returns `{"passed": True}`.  
> Post-MVP: Will delegate to a Tester-Worker with code analysis capabilities.

---

## Flow Diagrams

### Engineering Flow

```mermaid
sequenceDiagram
    participant User
    participant TG as telegram-bot
    participant PO as PO-Worker
    participant CLI as orchestrator-cli
    participant API
    participant Redis
    participant LG as langgraph
    participant Scaff as scaffolder

    User->>TG: "Сделай блог"
    TG->>PO: Forward via worker:po:{id}:input
    PO->>CLI: orchestrator project create --name blog --modules backend,telegram
    
    Note over CLI: Atomic operation
    CLI->>API: POST /api/projects {name, modules}
    API-->>CLI: project_id
    CLI->>API: POST /api/tasks {type=engineering}
    API-->>CLI: task_id
    CLI->>Redis: XADD engineering:queue
    CLI-->>PO: "✓ Engineering started (project: abc123)"
    
    Redis-->>LG: Consumer reads
    LG->>API: GET /api/projects/{id}
    API-->>LG: {status: CREATED, modules: [...]}
    LG->>Redis: XADD scaffolder:queue {project_id, modules}
    Redis-->>Scaff: Consumer reads
    Scaff->>Scaff: copier + git push
    Scaff->>API: PATCH /projects/{id} {status: SCAFFOLDED}
    Note over LG: Polls or listens for status change
    LG->>LG: Continue to Developer node
```

### Deploy Flow

```mermaid
sequenceDiagram
    participant PO as PO-Worker
    participant CLI as orchestrator-cli
    participant API
    participant Redis
    participant LG as langgraph
    participant Infra as infra-service

    PO->>CLI: orchestrator deploy start --project {id}
    CLI->>API: POST /api/tasks (type=deploy)
    CLI->>Redis: XADD deploy:queue
    Redis-->>LG: Consumer reads
    LG->>LG: DevOps Subgraph
    LG->>Redis: XADD ansible:deploy:queue
    Redis-->>Infra: Consumer reads
    Infra->>Infra: Run Ansible
    Infra->>Redis: XADD deploy:result:{request_id}
    Redis-->>LG: Resume graph
```

---

# Part 1: REST DTO

## ProjectDTO

```python
# shared/contracts/dto/project.py

from enum import Enum
from pydantic import BaseModel, ConfigDict

class ProjectStatus(str, Enum):
    DRAFT = "draft"
    SCAFFOLDING = "scaffolding"
    SCAFFOLDED = "scaffolded"
    DEVELOPING = "developing"
    TESTING = "testing"
    DEPLOYING = "deploying"
    ACTIVE = "active"
    FAILED = "failed"
    ARCHIVED = "archived"


class ServiceModule(str, Enum):
    """Available project modules for scaffolding."""
    BACKEND = "backend"
    TELEGRAM = "telegram"
    FRONTEND = "frontend"


class ProjectCreate(BaseModel):
    """Create project request."""
    name: str
    description: str | None = None
    modules: list[ServiceModule] = [ServiceModule.BACKEND]  # Default: backend only


class ProjectDTO(BaseModel):
    """Project response."""
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    name: str
    description: str | None = None
    status: ProjectStatus
    modules: list[ServiceModule] = []
    repository_url: str | None = None
    owner_id: int | None = None
```

## TaskDTO

```python
# shared/contracts/dto/task.py

from enum import Enum
from datetime import datetime
from pydantic import BaseModel, ConfigDict

class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    ENGINEERING = "engineering"
    DEPLOY = "deploy"


class TaskCreate(BaseModel):
    """Create task request."""
    project_id: str
    type: TaskType
    spec: str | None = None


class TaskDTO(BaseModel):
    """Task response."""
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    project_id: str
    type: TaskType
    status: TaskStatus
    spec: str | None = None
    result: dict | None = None
    created_at: datetime
    updated_at: datetime | None = None
```

## UserDTO

```python
# shared/contracts/dto/user.py

from pydantic import BaseModel, ConfigDict

class UserDTO(BaseModel):
    """User response."""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    telegram_id: int
    is_admin: bool = False
```

---

# Part 2: Queue Messages

## Base Types

```python
# shared/contracts/base.py

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
import uuid


class QueueMeta(BaseModel):
    """Metadata for all queue messages."""
    version: Literal["1"] = "1"
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class BaseMessage(QueueMeta):
    """Base class for queue messages."""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    callback_stream: str | None = None


class BaseResult(BaseModel):
    """Base result for async operations."""
    request_id: str
    status: Literal["success", "failed", "error", "timeout"]
    error: str | None = None
    duration_ms: int | None = None
```

---

## EngineeringMessage

**Queue:** `engineering:queue`  
**Initiator:** PO-Worker  
**Consumer:** langgraph

```python
# shared/contracts/queues/engineering.py

class EngineeringMessage(BaseMessage):
    """Start engineering task."""
    task_id: str
    project_id: str
    user_id: int


class EngineeringResult(BaseResult):
    """Engineering task result."""
    files_changed: list[str] | None = None
    commit_sha: str | None = None
    branch: str | None = None
```

---

## DeployMessage

**Queue:** `deploy:queue`  
**Initiator:** PO-Worker  
**Consumer:** langgraph

```python
# shared/contracts/queues/deploy.py

class DeployMessage(BaseMessage):
    """Start deploy task."""
    task_id: str
    project_id: str
    user_id: int


class DeployResult(BaseResult):
    """Deploy task result."""
    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None
```

---

## ScaffolderMessage

**Queue:** `scaffolder:queue`  
**Initiator:** langgraph (Engineering Subgraph)  
**Consumer:** scaffolder

```python
# shared/contracts/queues/scaffolder.py

class ScaffolderMessage(BaseMessage):
    """
    Initialize project structure.
    
    Responsibilities:
    1. Create remote repository (if not exists).
    2. Generate .project.yml config.
    3. Run copier template.
    4. Push initial commit.
    """
    project_id: str
    project_name: str
    modules: list[ServiceModule]



```

---

## WorkerCommand / WorkerResponse

**Queue (commands):** `worker:commands`  
**Initiator:** langgraph  
**Consumer:** worker-manager

**Queue (responses):** `worker:responses`  
**Initiator:** worker-manager  
**Consumer:** langgraph

```python
# shared/contracts/queues/worker.py

class AgentType(str, Enum):
    CLAUDE = "claude"          # Claude Code
    FACTORY = "factory"        # Factory.ai Droid


class WorkerCapability(str, Enum):
    GIT = "git"
    GITHUB_CLI = "github_cli"
    # Copier moved to dedicated service
    CURL = "curl"
    DOCKER = "docker"          # dind mount


class WorkerConfig(BaseModel):
    """Worker container configuration."""
    name: str
    worker_type: Literal["po", "developer"]  # Worker type for queue naming
    agent_type: AgentType                     # Which AI agent to use
    instructions: str                         # Content for CLAUDE.md / AGENTS.md
    allowed_commands: list[str]               # ["project.*", "engineering.start"]
    capabilities: list[WorkerCapability]      # ["git", "copier"]
    env_vars: dict[str, str] = {}


class CreateWorkerCommand(QueueMeta):
    """Create new worker."""
    command: Literal["create"] = "create"
    request_id: str
    config: WorkerConfig
    context: dict[str, str] = {}   # Additional context (user_id, task_id, etc.)


class DeleteWorkerCommand(QueueMeta):
    """Delete worker."""
    command: Literal["delete"] = "delete"
    request_id: str
    worker_id: str


class StatusWorkerCommand(QueueMeta):
    """Get worker status."""
    command: Literal["status"] = "status"
    request_id: str
    worker_id: str


WorkerCommand = CreateWorkerCommand | DeleteWorkerCommand | StatusWorkerCommand


class CreateWorkerResponse(BaseModel):
    """Response to create command."""
    request_id: str
    success: bool
    worker_id: str | None = None
    error: str | None = None


class DeleteWorkerResponse(BaseModel):
    """Response to delete command."""
    request_id: str
    success: bool
    error: str | None = None


class StatusWorkerResponse(BaseModel):
    """Response to status command."""
    request_id: str
    success: bool
    status: Literal["starting", "running", "stopped", "failed"] | None = None
    error: str | None = None


WorkerResponse = CreateWorkerResponse | DeleteWorkerResponse | StatusWorkerResponse
```

> **Note:** Message passing goes **directly** to worker queues (`worker:po:{id}:input`, etc.), 
> NOT through worker-manager. The manager handles only container lifecycle.

---

## WorkerLifecycleEvent

**Stream:** `worker:lifecycle`  
**Initiator:** worker-wrapper  
**Consumer:** worker-manager

```python
# shared/contracts/queues/worker_lifecycle.py

class WorkerLifecycleEvent(BaseModel):
    """Worker state change notification from wrapper."""
    worker_id: str
    event: Literal["started", "completed", "failed"]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    result: dict | None = None        # Agent output on success
    error: str | None = None          # Error message on failure
    exit_code: int | None = None
```

---

## PO Worker I/O

Коммуникация между Telegram Bot и Product Owner Worker.

**Queue (input):** `worker:po:{user_id}:input`  
**Initiator:** telegram-bot  
**Consumer:** worker-wrapper (inside PO container)

**Queue (output):** `worker:po:{user_id}:output`  
**Initiator:** worker-wrapper (inside PO container)  
**Consumer:** telegram-bot

```python
# shared/contracts/queues/po_worker.py

from datetime import datetime
from pydantic import BaseModel, Field
import uuid


class POWorkerInput(BaseModel):
    """Message from Telegram user to PO Worker."""
    
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: int                    # Telegram user ID
    text: str                       # User's message text
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class POWorkerOutput(BaseModel):
    """Response from PO Worker to Telegram user."""
    
    request_id: str                 # Matches input request_id
    user_id: int                    # Telegram user ID (for routing)
    text: str                       # PO's response text
    is_final: bool = True           # False if streaming (post-MVP)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
```

---

## Developer Worker I/O

Коммуникация между LangGraph (Engineering Subgraph) и Developer Worker.

**Queue (input):** `worker:developer:{task_id}:input`  
**Initiator:** langgraph (DeveloperNode)  
**Consumer:** worker-wrapper (inside Developer container)

**Queue (output):** `worker:developer:{task_id}:output`  
**Initiator:** worker-wrapper (inside Developer container)  
**Consumer:** langgraph (DeveloperNode)

```python
# shared/contracts/queues/developer_worker.py

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
import uuid


class DeveloperWorkerInput(BaseModel):
    """Task for Developer Worker from LangGraph."""
    
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str                    # Engineering task ID
    project_id: str                 # Project UUID
    prompt: str                     # Task specification
    timeout: int = 1800             # Max execution time (seconds)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class DeveloperWorkerOutput(BaseResult):
    """Result from Developer Worker to LangGraph."""
    
    # request_id, status, error, duration_ms inherited from BaseResult
    task_id: str                    # Engineering task ID
    result: str | None = None       # Agent's output (commit SHA, PR URL, etc.)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
```

---

## ProvisionerMessage

**Queue:** `provisioner:queue`  
**Initiator:** scheduler  
**Consumer:** infra-service

```python
# shared/contracts/queues/provisioner.py

class ProvisionerMessage(BaseMessage):
    """Provision server."""
    server_handle: str       # Cloud provider ID (Droplet ID) or unique identifier
    force_reinstall: bool = False
    is_recovery: bool = False


class ProvisionerResult(BaseResult):
    """
    Provisioning result.
    Stream: provisioner:results
    Consumers: scheduler (update DB), telegram-bot (notify admin)
    """
    server_handle: str
    server_ip: str | None = None
    services_redeployed: int = 0
    errors: list[str] | None = None
```

---

## AnsibleDeployMessage

**Queue:** `ansible:deploy:queue`  
**Initiator:** langgraph  
**Consumer:** infra-service

```python
# shared/contracts/queues/ansible_deploy.py

class AnsibleDeployMessage(BaseMessage):
    """Run ansible deploy."""
    job_type: Literal["deploy"] = "deploy"
    project_id: str
    project_name: str
    repo_full_name: str
    server_ip: str
    port: int
    modules: list[str] | None = None
    github_token_ref: str
    secrets_ref: str


class AnsibleDeployResult(BaseResult):
    """Ansible deploy result."""
    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
```

---

## ProgressEvent

**Stream:** `task_progress:{task_id}`  
**Initiator:** All consumers  
**Consumer:** telegram-bot

```python
# shared/contracts/events.py

class ProgressEvent(BaseModel):
    """Task progress notification."""
    type: Literal["started", "progress", "completed", "failed"]
    request_id: str
    task_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    message: str | None = None
    progress_pct: int | None = None
    current_step: str | None = None
    error: str | None = None
```

---

## File Structure

```
shared/contracts/
├── __init__.py
├── base.py                  # QueueMeta, BaseMessage, BaseResult
├── events.py                # ProgressEvent
├── dto/
│   ├── __init__.py
│   ├── project.py           # ProjectDTO, ProjectCreate
│   ├── task.py              # TaskDTO, TaskCreate
│   └── user.py              # UserDTO
└── queues/
    ├── __init__.py
    ├── engineering.py
    ├── deploy.py
    ├── scaffolder.py
    ├── provisioner.py
    ├── ansible_deploy.py
    ├── worker.py
    └── worker_io.py
```
