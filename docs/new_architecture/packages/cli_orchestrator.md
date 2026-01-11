# Tool: CLI Orchestrator

**Package Name:** `orchestrator-cli`
**Current Location:** `shared/cli/` (requires extraction to `packages/orchestrator-cli/`)
**Responsibility:** Agent-to-System interface for Workers.

## 1. Philosophy

The CLI is the **only interface** between AI Agents inside Worker containers and the orchestration system. It replaces direct API calls and provides structured, validated commands.

> **Rule #1:** CLI is for Agents only — no human interaction expected.
> **Rule #2:** All output is JSON (for headless parsing).
> **Rule #3:** Commands are permission-checked via `ALLOWED_COMMANDS` env var.

## 2. Responsibilities

1.  **API Wrapper**: Translate CLI commands to REST API calls.
2.  **Event Publisher**: Publish messages to Redis queues (engineering, deploy).
    Note: Deploy is consumed by LangGraph which triggers GitHub Actions.
3.  **Permission Enforcement**: Check `ALLOWED_COMMANDS` before executing.
4.  **Validation**: Pydantic validation with clear error messages.

## 3. Commands

### 3.1 Project Management

| Command | Action | API/Redis |
|---------|--------|-----------|
| `orchestrator project create --name <name> [--modules <m1,m2>]` | Create project | POST `/api/projects` |
| `orchestrator project list` | List user's projects | GET `/api/projects` |
| `orchestrator project get <id>` | Get project details | GET `/api/projects/{id}` |
| `orchestrator project tasks <id>` | List project tasks | GET `/api/tasks?project_id={id}` |

### 3.2 Engineering

| Command | Action | API/Redis |
|---------|--------|-----------|
| `orchestrator engineering start --project <id> --spec <text>` | Start engineering task | POST `/api/tasks` + XADD `engineering:queue` |
| `orchestrator engineering status <task_id>` | Get task status | GET `/api/tasks/{id}` |
| `orchestrator engineering wait <task_id> [--timeout 3600]` | Poll until complete | GET `/api/tasks/{id}` (loop) |

### 3.3 Deploy

| Command | Action | API/Redis |
|---------|--------|-----------|
| `orchestrator deploy start --project <id>` | Start deploy task | POST `/api/tasks` + XADD `deploy:queue` |
| `orchestrator deploy status <task_id>` | Get deploy status | GET `/api/tasks/{id}` |

### 3.4 Infrastructure

| Command | Action | API/Redis |
|---------|--------|-----------|
| `orchestrator infra servers` | List servers | GET `/api/servers` |
| `orchestrator infra incidents` | List active incidents | GET `/api/incidents` |

### 3.5 Communication

| Command | Action | API/Redis |
|---------|--------|-----------|
| `orchestrator respond --message <text>` | Send message to user | XADD `worker:{id}:output` |

### 3.6 Diagnostics

| Command | Action | API/Redis |
|---------|--------|-----------|
| `orchestrator diagnose api` | Check API health | GET `/api/health` |
| `orchestrator diagnose redis` | Check Redis connection | PING |
| `orchestrator diagnose projects` | Validate project states | GET `/api/projects` + checks |

## 4. Permission Model

### 4.1 Environment Variable

Workers receive `ALLOWED_COMMANDS` in their environment:

```bash
ALLOWED_COMMANDS="project.get,project.list,engineering.status,respond"
```

### 4.2 Format

- Comma-separated list of `<group>.<command>` patterns
- Wildcards: `*` = all commands, `project.*` = all project commands
- Empty/missing = `*` (backwards compatibility, PO default)

### 4.3 Enforcement

```python
# orchestrator/permissions.py

import os

COMMAND_PERMISSION_MAP = {
    "project create": "project.create",
    "project list": "project.list",
    "project get": "project.get",
    "engineering start": "engineering.start",
    "engineering status": "engineering.status",
    "deploy start": "deploy.start",
    # ...
}

def check_permission(command_path: str) -> bool:
    """Check if current worker is allowed to run this command."""
    allowed_raw = os.environ.get("ALLOWED_COMMANDS", "*")
    if allowed_raw == "*":
        return True
    
    allowed = set(allowed_raw.split(","))
    permission = COMMAND_PERMISSION_MAP.get(command_path)
    
    if permission in allowed:
        return True
    
    # Check wildcard patterns (e.g., "project.*")
    group = permission.split(".")[0] + ".*"
    return group in allowed


def require_permission(command_path: str):
    """Decorator/check that raises if permission denied."""
    if not check_permission(command_path):
        raise PermissionError(
            f"Command '{command_path}' not allowed. "
            f"Allowed: {os.environ.get('ALLOWED_COMMANDS', '*')}"
        )
```

### 4.4 Predefined Profiles (Convenience)

| Profile | Commands |
|---------|----------|
| `PRODUCT_OWNER` | `*` (all) |
| `DEVELOPER` | `project.get`, `project.list`, `engineering.status`, `respond` |
| `DEVOPS` | `project.get`, `deploy.*`, `infra.*`, `respond` |

Worker-manager can expand profile names to command lists when spawning.

## 5. Output Format

All commands output **JSON only** (for agent parsing):

### Success
```json
{
  "success": true,
  "data": { ... },
  "message": "Project created successfully"
}
```

### Error
```json
{
  "success": false,
  "error": "ValidationError",
  "message": "Field 'name' is required",
  "details": { "field": "name", "constraint": "required" }
}
```

### Permission Denied
```json
{
  "success": false,
  "error": "PermissionDenied",
  "message": "Command 'deploy.start' not allowed",
  "allowed_commands": ["project.get", "project.list"]
}
```

## 6. Redis Publishing

### 6.1 Current State
CLI currently only calls REST API. Queue publishing is done by API internally.

### 6.2 Target State
CLI publishes directly to Redis queues:

```python
# orchestrator/commands/engineering.py

async def start(project_id: str, spec: str):
    # 1. Create Task via API
    task = await api_client.post("/api/tasks", {
        "project_id": project_id,
        "type": "engineering",
        "status": "queued"
    })
    
    # 2. Publish to queue (NEW)
    message = EngineeringMessage(
        task_id=task["id"],
        project_id=project_id,
        user_id=get_user_id(),
    )
    await redis_client.xadd("engineering:queue", message.model_dump())
    
    return {"task_id": task["id"], "status": "queued"}
```

### 6.3 Why CLI publishes?

- API becomes "thin" CRUD layer (no Redis dependency)
- CLI is the orchestration point for agents
- Single responsibility: API = data, CLI = workflow

## 7. Dependencies

**Required:**
*   `typer` (CLI framework)
*   `httpx` / `aiohttp` (API client)
*   `redis` (queue publishing)
*   `pydantic` (validation)
*   `rich` (for JSON formatting)

## 8. Installation

CLI is installed in `worker-base` image:

```dockerfile
# docker/Dockerfile.worker-base
COPY packages/orchestrator-cli /app/orchestrator-cli
RUN pip install /app/orchestrator-cli
```

Agent invokes via:
```bash
orchestrator project create --name my-bot
```

## 9. Migration Notes

### 9.1 Extract from shared/
- Move `shared/cli/` → `packages/orchestrator-cli/`
- Update imports in any services that use CLI (should be none)
- Update `worker-base` Dockerfile

### 9.2 Add Redis publishing
- Add `redis` dependency
- Create `redis_client.py` with async connection
- Modify `engineering.py`, `deploy.py` to publish after API call

### 9.3 Add permission checks
- Create `permissions.py` module
- Add `@require_permission` decorator to each command
- Pass `ALLOWED_COMMANDS` in `WorkerConfig.env_vars`
