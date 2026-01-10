# Contracts / Контракты

Типизированные схемы для REST API и Redis очередей.

## Design Principles

1. **Schema-first** — все сообщения валидируются Pydantic схемами
2. **1:1 Queues** — одна очередь = один Writer → один Consumer (+ optional observers)
3. **Logical Actors** — указываем роль (PO-Worker, langgraph), не техническую прослойку
4. **Traceable** — `correlation_id` для сквозной трассировки

---

## Queue Overview

| Queue | DTO | Writer | Consumer | Purpose |
|-------|-----|--------|----------|---------|
| `engineering:queue` | EngineeringMessage | PO-Worker | langgraph | Start development task |
| `deploy:queue` | DeployMessage | PO-Worker | langgraph | Start deploy task |
| `scaffolder:queue` | ScaffolderMessage | langgraph | scaffolder | Init project structure |
| `worker:commands` | WorkerCommand | langgraph | worker-manager | Spawn/kill workers |
| `worker:responses` | WorkerResponse | worker-manager | langgraph | Worker lifecycle events |
| `worker:{id}:input` | WorkerInputMessage | telegram-bot | worker-wrapper | User message to agent |
| `worker:{id}:output` | WorkerOutputMessage | worker-wrapper | telegram-bot | Agent reply to user |
| `provisioner:queue` | ProvisionerMessage | scheduler | infra-service | Setup server |
| `ansible:deploy:queue` | AnsibleDeployMessage | langgraph | infra-service | Run ansible deploy |

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


class ProjectCreate(BaseModel):
    """Create project request."""
    name: str
    description: str | None = None


class ProjectDTO(BaseModel):
    """Project response."""
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    name: str
    description: str | None = None
    status: ProjectStatus
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
    SCAFFOLDING = "scaffolding"


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
    status: Literal["success", "failed", "error"]
    error: str | None = None
    duration_ms: int | None = None
```

---

## EngineeringMessage

**Queue:** `engineering:queue`  
**Writer:** PO-Worker  
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
**Writer:** PO-Worker  
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
**Writer:** langgraph  
**Consumer:** scaffolder

```python
# shared/contracts/queues/scaffolder.py

class ScaffolderMessage(BaseMessage):
    """Initialize project structure."""
    project_id: str
    repo_full_name: str      # "org/repo"
    project_name: str
    modules: list[str]       # ["backend", "telegram"]
```

---

## WorkerCommand / WorkerResponse

**Queue (commands):** `worker:commands`  
**Writer:** langgraph  
**Consumer:** worker-manager

**Queue (responses):** `worker:responses`  
**Writer:** worker-manager  
**Consumer:** langgraph

```python
# shared/contracts/queues/worker.py

class WorkerConfig(BaseModel):
    """Worker container configuration."""
    name: str
    agent: Literal["claude-code", "factory-droid"]
    capabilities: list[str]        # ["git", "github", "python"]
    env_vars: dict[str, str]
    mount_session_volume: bool = False


class CreateWorkerCommand(QueueMeta):
    """Create new worker."""
    command: Literal["create"] = "create"
    request_id: str
    config: WorkerConfig
    context: dict[str, str] = {}


class SendMessageCommand(QueueMeta):
    """Send message to worker."""
    command: Literal["send_message"] = "send_message"
    request_id: str
    worker_id: str
    message: str
    timeout: int = 1800


class DeleteWorkerCommand(QueueMeta):
    """Delete worker."""
    command: Literal["delete"] = "delete"
    request_id: str
    worker_id: str


WorkerCommand = CreateWorkerCommand | SendMessageCommand | DeleteWorkerCommand


class CreateWorkerResponse(BaseModel):
    """Response to create command."""
    request_id: str
    success: bool
    worker_id: str | None = None
    error: str | None = None


class SendMessageResponse(BaseModel):
    """Response to send_message command."""
    request_id: str
    success: bool
    response: str | None = None
    error: str | None = None


class DeleteWorkerResponse(BaseModel):
    """Response to delete command."""
    request_id: str
    success: bool
    error: str | None = None


WorkerResponse = CreateWorkerResponse | SendMessageResponse | DeleteWorkerResponse
```

---

## WorkerInputMessage / WorkerOutputMessage

**Queue (input):** `worker:{id}:input`  
**Writer:** telegram-bot  
**Consumer:** worker-wrapper

**Queue (output):** `worker:{id}:output`  
**Writer:** worker-wrapper  
**Consumer:** telegram-bot

```python
# shared/contracts/queues/worker_io.py

class WorkerInputMessage(BaseModel):
    """Message to worker agent."""
    request_id: str
    prompt: str
    timeout: int = 1800


class WorkerOutputMessage(BaseModel):
    """Response from worker agent."""
    request_id: str
    status: Literal["success", "error", "timeout"]
    response: str | None = None
    error: str | None = None
    duration_ms: int
```

---

## ProvisionerMessage

**Queue:** `provisioner:queue`  
**Writer:** scheduler  
**Consumer:** infra-service

```python
# shared/contracts/queues/provisioner.py

class ProvisionerMessage(BaseMessage):
    """Provision server."""
    server_handle: str
    force_reinstall: bool = False
    is_recovery: bool = False


class ProvisionerResult(BaseResult):
    """Provisioning result."""
    server_handle: str
    server_ip: str | None = None
    services_redeployed: int = 0
    errors: list[str] | None = None
```

---

## AnsibleDeployMessage

**Queue:** `ansible:deploy:queue`  
**Writer:** langgraph  
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
**Writer:** All consumers  
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
