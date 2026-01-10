# Queue Contracts / Контракты очередей

Типизированные схемы для всех Redis очередей.

## Design Principles

1. **Schema-first** — все сообщения валидируются Pydantic схемами
2. **Versioned** — каждое сообщение содержит версию для backwards compatibility
3. **Traceable** — correlation_id для сквозной трассировки
4. **No secrets in plaintext** — токены передаются по ссылке, не напрямую

---

## Base Types

```python
# shared/contracts/base.py

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
import uuid


class QueueMeta(BaseModel):
    """Metadata для всех queue messages."""

    version: Literal["1"] = "1"
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class BaseMessage(QueueMeta):
    """Базовый класс для сообщений в очередях."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    callback_stream: str | None = None


class BaseResult(BaseModel):
    """Базовый результат выполнения."""

    request_id: str
    status: Literal["success", "failed", "error"]
    error: str | None = None
    duration_ms: int | None = None
```

---

## Queue: `engineering:queue`

**Producer:** CLI, Telegram Bot (через API)
**Consumer:** `engineering-consumer`
**Purpose:** Запуск Engineering Subgraph

### Message

```python
# shared/contracts/queues/engineering.py

class EngineeringMessage(BaseMessage):
    """Сообщение в engineering:queue."""

    task_id: str          # ID Task в БД
    project_id: str       # ID Project
    user_id: int          # Telegram user ID
```

### Result

Результат сохраняется в `Task.result`:

```python
class EngineeringResult(BaseResult):
    """Результат engineering задачи."""

    files_changed: list[str] | None = None
    commit_sha: str | None = None
    branch: str | None = None
```

---

## Queue: `deploy:queue`

**Producer:** CLI, Telegram Bot
**Consumer:** `deploy-consumer`
**Purpose:** Запуск DevOps Subgraph

### Message

```python
# shared/contracts/queues/deploy.py

class DeployMessage(BaseMessage):
    """Сообщение в deploy:queue."""

    task_id: str
    project_id: str
    user_id: int
```

### Result

Результат сохраняется в `Task.result`:

```python
class DeployResult(BaseResult):
    """Результат deploy задачи."""

    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None
```

---

## Queue: `scaffolder:queue`

**Producer:** LangGraph Tools
**Consumer:** `scaffolder`
**Purpose:** Scaffolding проекта через Copier

### Message

```python
# shared/contracts/queues/scaffolder.py

class ScaffolderMessage(BaseMessage):
    """Сообщение в scaffolder:queue."""

    project_id: str
    repo_full_name: str      # "org/repo"
    project_name: str        # Human-readable name
    modules: list[str]       # ["backend", "telegram"]
```

### Result

Результат — обновление статуса Project через API:
- Success: `status = "scaffolded"`
- Failure: `status = "scaffold_failed"`

---

## Queue: `provisioner:queue`

**Producer:** Scheduler, LangGraph
**Consumer:** `infra-consumer`
**Purpose:** Провизия серверов (Ansible)

### Message

```python
# shared/contracts/queues/provisioner.py

class ProvisionerMessage(BaseMessage):
    """Сообщение в provisioner:queue."""

    server_handle: str
    force_reinstall: bool = False
    is_recovery: bool = False
```

### Result

Результат записывается в Redis key `provisioner:result:{request_id}` (TTL 1 hour):

```python
class ProvisionerResult(BaseResult):
    """Результат провизии сервера."""

    server_handle: str
    server_ip: str | None = None
    services_redeployed: int = 0
    errors: list[str] | None = None
```

---

## Queue: `ansible:deploy:queue`

**Producer:** DevOps Subgraph (DeployerNode)
**Consumer:** `infra-consumer`
**Purpose:** Делегированный Ansible деплой

### Message

```python
# shared/contracts/queues/ansible_deploy.py

class AnsibleDeployMessage(BaseMessage):
    """Сообщение в ansible:deploy:queue."""

    job_type: Literal["deploy"] = "deploy"
    project_id: str
    project_name: str              # snake_case
    repo_full_name: str            # "owner/repo"
    server_ip: str
    port: int
    modules: list[str] | None = None

    # Secrets passed by reference, not value
    github_token_ref: str          # Reference to secret storage
    secrets_ref: str               # Reference to project secrets
```

### Result

Результат записывается в Redis key `deploy:result:{request_id}` (TTL 1 hour):

```python
class AnsibleDeployResult(BaseResult):
    """Результат Ansible деплоя."""

    deployed_url: str | None = None
    server_ip: str | None = None
    port: int | None = None

    # Debug info (only on failure)
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
```

---

## Queue: `worker:commands`

**Producer:** LangGraph Nodes
**Consumer:** `worker-manager`
**Purpose:** Управление Worker контейнерами

### Commands

```python
# shared/contracts/queues/worker.py

class WorkerConfig(BaseModel):
    """Конфигурация Worker."""

    name: str
    agent: Literal["claude-code", "factory-droid"]
    capabilities: list[str]        # ["git", "github", "python", "node"]
    env_vars: dict[str, str]
    mount_session_volume: bool = False


class CreateWorkerCommand(QueueMeta):
    """Создать новый Worker."""

    command: Literal["create"] = "create"
    request_id: str
    config: WorkerConfig
    context: dict[str, str] = {}


class SendMessageCommand(QueueMeta):
    """Отправить сообщение в Worker."""

    command: Literal["send_message"] = "send_message"
    request_id: str
    worker_id: str                 # ID созданного worker
    message: str
    timeout: int = 1800            # seconds


class SendFileCommand(QueueMeta):
    """Отправить файл в Worker."""

    command: Literal["send_file"] = "send_file"
    request_id: str
    worker_id: str
    path: str                      # Path inside container
    content: str


class DeleteWorkerCommand(QueueMeta):
    """Удалить Worker."""

    command: Literal["delete"] = "delete"
    request_id: str
    worker_id: str


# Union type для всех команд
WorkerCommand = CreateWorkerCommand | SendMessageCommand | SendFileCommand | DeleteWorkerCommand
```

---

## Queue: `worker:responses`

**Producer:** `worker-manager`
**Consumer:** LangGraph Nodes (per-request consumer groups)
**Purpose:** Ответы от Worker Manager

### Responses

```python
# shared/contracts/queues/worker.py

class CreateWorkerResponse(BaseModel):
    """Ответ на create команду."""

    request_id: str
    success: bool
    worker_id: str | None = None
    error: str | None = None


class SendMessageResponse(BaseModel):
    """Ответ на send_message команду."""

    request_id: str
    success: bool
    response: str | None = None    # CLI-Agent response
    error: str | None = None


class DeleteWorkerResponse(BaseModel):
    """Ответ на delete команду."""

    request_id: str
    success: bool
    error: str | None = None


WorkerResponse = CreateWorkerResponse | SendMessageResponse | DeleteWorkerResponse
```

---

## Events: `callback_stream`

**Producer:** All Consumers
**Consumer:** Telegram Bot
**Purpose:** Real-time progress updates

### Event Schema

```python
# shared/contracts/events.py

class ProgressEvent(BaseModel):
    """Событие прогресса выполнения Task."""

    type: Literal["started", "progress", "completed", "failed"]
    request_id: str
    task_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Progress details
    message: str | None = None
    progress_pct: int | None = None    # 0-100
    current_step: str | None = None

    # Error details (for failed)
    error: str | None = None
    error_type: str | None = None
```

---

## File Structure

```
shared/
├── contracts/
│   ├── __init__.py              # Re-exports all contracts
│   ├── base.py                  # QueueMeta, BaseMessage, BaseResult
│   ├── events.py                # ProgressEvent
│   └── queues/
│       ├── __init__.py
│       ├── engineering.py       # EngineeringMessage, EngineeringResult
│       ├── deploy.py            # DeployMessage, DeployResult
│       ├── scaffolder.py        # ScaffolderMessage
│       ├── provisioner.py       # ProvisionerMessage, ProvisionerResult
│       ├── ansible_deploy.py    # AnsibleDeployMessage, AnsibleDeployResult
│       └── worker.py            # WorkerCommand, WorkerResponse
```

---

## Usage Example

### Publishing

```python
from shared.contracts.queues.engineering import EngineeringMessage
from shared.queue_client import publish_message

message = EngineeringMessage(
    task_id="task_abc123",
    project_id="proj_xyz",
    user_id=12345,
    callback_stream="task_progress:task_abc123",
)

await publish_message("engineering:queue", message)
```

### Consuming

```python
from shared.contracts.queues.engineering import EngineeringMessage
from shared.queue_client import consume_messages

async for raw_message in consume_messages("engineering:queue", "engineering-consumer"):
    message = EngineeringMessage.model_validate(raw_message)
    await process_engineering_task(message)
```

---

## Migration Notes

### Breaking Changes from Current Format

1. **Wrapper removal**: Currently messages are wrapped as `{"data": json.dumps(...)}`. New format is direct JSON.

2. **Field renames**:
   - `agent_id` → `worker_id` (in worker commands)
   - `cli-agent:*` → `worker:*` (queue names)

3. **New required fields**:
   - `version` — for schema versioning
   - `correlation_id` — for distributed tracing
   - `timestamp` — for debugging

4. **Secrets by reference**:
   - `github_token` → `github_token_ref`
   - `secrets` → `secrets_ref`

### Backwards Compatibility Strategy

During migration, consumers should accept both formats:

```python
def parse_message(raw: dict) -> EngineeringMessage:
    # Handle legacy wrapper format
    if "data" in raw and isinstance(raw["data"], str):
        raw = json.loads(raw["data"])

    # Handle missing version (legacy)
    if "version" not in raw:
        raw["version"] = "1"
        raw["correlation_id"] = raw.get("correlation_id", str(uuid.uuid4()))
        raw["timestamp"] = raw.get("timestamp", datetime.utcnow().isoformat())

    return EngineeringMessage.model_validate(raw)
```
