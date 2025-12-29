# Phase 4: Capability Tools — Detailed Implementation Plan

> Part of [Dynamic ProductOwner Design](./dynamic-po-design.md)

## Overview

Phase 4 implements the actual capability tools that PO can use. Key decision: **LangGraph + Redis Queue** architecture for reliable async job execution.

After this phase, PO can:
- Trigger deployments and monitor their progress
- Manage infrastructure resources
- View logs and diagnose issues
- Manually control graph nodes (admin)

---

## Architecture Decision: LangGraph + Redis Queue

### Why Not Pure LangGraph?

Pure `asyncio.create_task()` approach has critical issues:
- Worker restart = task lost, no retry
- No backpressure (100 deploys = 100 parallel tasks)
- No delivery guarantee

### Hybrid Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         PO Agentic Loop                         │
│                                                                 │
│  trigger_deploy(project_id)                                     │
│       │                                                         │
│       ▼                                                         │
│  ┌─────────────────┐                                            │
│  │ Generate job_id │                                            │
│  │ Publish to      │──────┐                                     │
│  │ deploy:queue    │      │                                     │
│  └─────────────────┘      │                                     │
│       │                   │                                     │
│       ▼                   │                                     │
│  Return {job_id, status}  │                                     │
│                           │                                     │
│  get_deploy_status(job_id)│                                     │
│       │                   │                                     │
│       ▼                   │                                     │
│  Read from checkpointer   │                                     │
│  (thread_id = job_id)     │                                     │
└───────────────────────────┼─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Redis Stream: deploy:queue                   │
│                                                                 │
│  Consumer Group: deploy-workers                                 │
└───────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Deploy Worker Process                      │
│                                                                 │
│  while True:                                                    │
│      job = redis.xreadgroup("deploy:queue", ...)                │
│      await devops_subgraph.ainvoke(                             │
│          state,                                                 │
│          {"configurable": {"thread_id": job["job_id"]}}         │
│      )                                                          │
│      redis.xack(...)                                            │
│                                                                 │
│  Checkpoints saved to PostgreSQL ─────────────────────┐         │
└───────────────────────────────────────────────────────┼─────────┘
                                                        │
                                                        ▼
                                              ┌─────────────────┐
                                              │   Checkpointer  │
                                              │   (PostgreSQL)  │
                                              └─────────────────┘
```

### Benefits

| Benefit | How |
|---------|-----|
| **Reliable delivery** | Redis Stream with consumer groups, unacked jobs re-delivered |
| **Worker crash recovery** | Job stays in queue until ACK |
| **Scalability** | N workers can consume from same queue |
| **State persistence** | LangGraph checkpointer (PostgreSQL) stores full job state |
| **Polling via checkpointer** | `get_deploy_status` reads from same checkpointer |
| **Unified model** | DevOps runs as LangGraph subgraph, same patterns as PO |

---

## Redis Streams Setup

### Stream Names

```python
# In shared/redis_client.py or new file
DEPLOY_QUEUE = "deploy:queue"
ENGINEERING_QUEUE = "engineering:queue"
```

### Consumer Group Creation

```python
# On worker startup
async def ensure_consumer_groups(redis: Redis):
    """Create consumer groups if not exist."""
    for stream in [DEPLOY_QUEUE, ENGINEERING_QUEUE]:
        try:
            await redis.xgroup_create(
                stream,
                groupname="workers",
                id="0",
                mkstream=True,
            )
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
```

---

## 4.1 Deploy Capability

### Tools

#### trigger_deploy

```python
# services/langgraph/src/tools/deploy.py

@tool
async def trigger_deploy(project_id: str) -> dict:
    """
    Start deployment for a project.

    Prerequisites (checked automatically):
    - Project exists and has repository
    - Resources allocated (server + port)
    - CI passed (if configured)

    Args:
        project_id: Project identifier

    Returns:
        {"job_id": "deploy_xxx", "status": "queued"}
        or {"error": "...", "missing": [...]} if not ready

    After calling, use get_deploy_status(job_id) to monitor progress.
    """
    # 1. Check readiness
    readiness = await check_deploy_readiness.ainvoke({"project_id": project_id})
    if not readiness.get("ready"):
        return {
            "error": "Project not ready for deployment",
            "missing": readiness.get("missing", []),
        }

    # 2. Generate job_id (will be thread_id for subgraph)
    job_id = f"deploy_{project_id}_{uuid4().hex[:8]}"

    # 3. Publish to queue
    redis = await get_redis_client()
    await redis.xadd(
        DEPLOY_QUEUE,
        {
            "job_id": job_id,
            "project_id": project_id,
            "user_id": str(_context.state["telegram_user_id"]),
            "correlation_id": _context.state.get("correlation_id", ""),
            "queued_at": datetime.utcnow().isoformat(),
        },
    )

    logger.info("deploy_queued", job_id=job_id, project_id=project_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "message": f"Deployment queued. Use get_deploy_status('{job_id}') to check progress.",
    }
```

#### get_deploy_status

```python
@tool
async def get_deploy_status(job_id: str) -> dict:
    """
    Check deployment progress.

    Args:
        job_id: Job ID returned by trigger_deploy

    Returns:
        {
            "status": "queued|running|success|failed",
            "progress": "Building Docker image...",
            "logs_tail": "...",
            "deployed_url": "http://...",  # if success
            "error": "...",                 # if failed
        }
    """
    # Read from LangGraph checkpointer
    config = {"configurable": {"thread_id": job_id}}

    checkpoint = await checkpointer.aget(config)

    if not checkpoint:
        return {"status": "not_found", "error": f"No deployment with job_id={job_id}"}

    state = checkpoint.get("channel_values", {})

    return {
        "status": state.get("deploy_status", "unknown"),
        "progress": state.get("deploy_progress", ""),
        "logs_tail": state.get("deploy_logs_tail", "")[-500:],  # Last 500 chars
        "deployed_url": state.get("deployed_url"),
        "error": state.get("deploy_error"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
    }
```

#### get_deploy_logs

```python
@tool
async def get_deploy_logs(job_id: str, lines: int = 100) -> dict:
    """
    Get full deployment logs.

    Args:
        job_id: Job ID from trigger_deploy
        lines: Number of lines to return (default 100, max 1000)

    Returns:
        {"logs": "...", "status": "running|success|failed"}
    """
    lines = min(lines, 1000)

    config = {"configurable": {"thread_id": job_id}}
    checkpoint = await checkpointer.aget(config)

    if not checkpoint:
        return {"error": f"No deployment with job_id={job_id}"}

    state = checkpoint.get("channel_values", {})
    full_logs = state.get("deploy_logs", "")

    # Get last N lines
    log_lines = full_logs.split("\n")
    tail = "\n".join(log_lines[-lines:])

    return {
        "logs": tail,
        "status": state.get("deploy_status", "unknown"),
        "total_lines": len(log_lines),
    }
```

#### check_deploy_readiness (update existing)

```python
@tool
async def check_deploy_readiness(project_id: str) -> dict:
    """
    Check if project is ready for deployment.

    Checks:
    - Project exists
    - Repository configured
    - Resources allocated (server + port)
    - CI status (if applicable)

    Returns:
        {"ready": True} or {"ready": False, "missing": ["allocated_resources", ...]}
    """
    missing = []

    # 1. Get project
    project = await api_client.get_project(project_id)
    if not project:
        return {"ready": False, "missing": ["project_not_found"]}

    # 2. Check repository
    if not project.get("repository_url"):
        missing.append("repository")

    # 3. Check resources
    allocations = await api_client.get_project_allocations(project_id)
    if not allocations:
        missing.append("allocated_resources")

    # 4. Check CI (optional)
    if project.get("ci_required"):
        ci_status = await api_client.get_ci_status(project_id)
        if ci_status != "passed":
            missing.append("ci_passed")

    return {
        "ready": len(missing) == 0,
        "missing": missing,
        "project_name": project.get("name"),
        "server": allocations[0].get("server_handle") if allocations else None,
        "port": allocations[0].get("port") if allocations else None,
    }
```

### Deploy Worker

**Location**: `services/langgraph/src/workers/deploy_worker.py`

```python
"""
Deploy Worker — consumes from deploy:queue and runs DevOps subgraph.

Run: python -m src.workers.deploy_worker
"""

import asyncio
import structlog
from redis.asyncio import Redis

from shared.redis_client import get_redis_client
from ..graph.devops_subgraph import devops_graph
from ..checkpointer import get_checkpointer

logger = structlog.get_logger()

DEPLOY_QUEUE = "deploy:queue"
CONSUMER_GROUP = "workers"
CONSUMER_NAME = f"worker-{os.getpid()}"


async def process_deploy_job(job_data: dict) -> None:
    """Run DevOps subgraph for a single deploy job."""
    job_id = job_data["job_id"]
    project_id = job_data["project_id"]

    logger.info("deploy_started", job_id=job_id, project_id=project_id)

    checkpointer = await get_checkpointer()
    config = {"configurable": {"thread_id": job_id}}

    initial_state = {
        "project_id": project_id,
        "deploy_status": "running",
        "deploy_progress": "Starting deployment...",
        "deploy_logs": "",
        "started_at": datetime.utcnow().isoformat(),
        "correlation_id": job_data.get("correlation_id"),
    }

    try:
        result = await devops_graph.ainvoke(initial_state, config)

        logger.info(
            "deploy_finished",
            job_id=job_id,
            status=result.get("deploy_status"),
            url=result.get("deployed_url"),
        )

    except Exception as e:
        logger.error("deploy_failed", job_id=job_id, error=str(e))

        # Update checkpoint with error
        error_state = {
            **initial_state,
            "deploy_status": "failed",
            "deploy_error": str(e),
            "finished_at": datetime.utcnow().isoformat(),
        }
        await checkpointer.aput(config, error_state)


async def run_worker():
    """Main worker loop."""
    redis = await get_redis_client()

    # Ensure consumer group exists
    try:
        await redis.xgroup_create(DEPLOY_QUEUE, CONSUMER_GROUP, id="0", mkstream=True)
    except Exception:
        pass  # Group already exists

    logger.info("deploy_worker_started", consumer=CONSUMER_NAME)

    while True:
        try:
            # Read from stream (block up to 5 seconds)
            messages = await redis.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={DEPLOY_QUEUE: ">"},
                count=1,
                block=5000,
            )

            if not messages:
                continue

            for stream, entries in messages:
                for entry_id, data in entries:
                    try:
                        await process_deploy_job(data)
                        await redis.xack(DEPLOY_QUEUE, CONSUMER_GROUP, entry_id)
                    except Exception as e:
                        logger.error(
                            "deploy_job_error",
                            entry_id=entry_id,
                            error=str(e),
                        )
                        # Don't ACK — job will be redelivered

        except asyncio.CancelledError:
            logger.info("deploy_worker_shutdown")
            break
        except Exception as e:
            logger.error("deploy_worker_error", error=str(e))
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run_worker())
```

### DevOps Subgraph

**Location**: `services/langgraph/src/graph/devops_subgraph.py`

```python
"""
DevOps Subgraph — handles actual deployment logic.

State is checkpointed, allowing:
- Progress polling via get_deploy_status
- Resume on worker crash
- Full audit trail
"""

from typing import TypedDict
from langgraph.graph import StateGraph, END


class DevOpsState(TypedDict):
    # Input
    project_id: str
    correlation_id: str | None

    # Progress
    deploy_status: str        # running, success, failed
    deploy_progress: str      # Human-readable progress
    deploy_logs: str          # Full logs
    deploy_logs_tail: str     # Last N lines for quick view

    # Timing
    started_at: str
    finished_at: str | None

    # Result
    deployed_url: str | None
    deploy_error: str | None


async def fetch_project_config(state: DevOpsState) -> DevOpsState:
    """Load project config from API."""
    project = await api_client.get_project(state["project_id"])
    allocations = await api_client.get_project_allocations(state["project_id"])

    return {
        **state,
        "deploy_progress": "Fetched project configuration",
        "deploy_logs": state["deploy_logs"] + "\n[INFO] Loaded project config",
        "_project": project,
        "_allocation": allocations[0] if allocations else None,
    }


async def build_and_push(state: DevOpsState) -> DevOpsState:
    """Build Docker image and push to registry."""
    # ... actual build logic ...
    return {
        **state,
        "deploy_progress": "Building Docker image...",
        "deploy_logs": state["deploy_logs"] + "\n[INFO] Docker build started",
    }


async def run_ansible_deploy(state: DevOpsState) -> DevOpsState:
    """Run Ansible playbook to deploy."""
    # ... actual ansible logic ...
    return {
        **state,
        "deploy_progress": "Running Ansible deployment...",
    }


async def verify_deployment(state: DevOpsState) -> DevOpsState:
    """Health check deployed service."""
    # ... health check logic ...

    allocation = state.get("_allocation", {})
    url = f"http://{allocation.get('server_ip')}:{allocation.get('port')}"

    return {
        **state,
        "deploy_status": "success",
        "deploy_progress": "Deployment complete!",
        "deployed_url": url,
        "finished_at": datetime.utcnow().isoformat(),
    }


async def handle_failure(state: DevOpsState) -> DevOpsState:
    """Handle deployment failure."""
    return {
        **state,
        "deploy_status": "failed",
        "finished_at": datetime.utcnow().isoformat(),
    }


def route_after_build(state: DevOpsState) -> str:
    """Check if build succeeded."""
    if state.get("deploy_error"):
        return "handle_failure"
    return "run_ansible_deploy"


# Build graph
graph = StateGraph(DevOpsState)

graph.add_node("fetch_project_config", fetch_project_config)
graph.add_node("build_and_push", build_and_push)
graph.add_node("run_ansible_deploy", run_ansible_deploy)
graph.add_node("verify_deployment", verify_deployment)
graph.add_node("handle_failure", handle_failure)

graph.set_entry_point("fetch_project_config")
graph.add_edge("fetch_project_config", "build_and_push")
graph.add_conditional_edges("build_and_push", route_after_build)
graph.add_edge("run_ansible_deploy", "verify_deployment")
graph.add_edge("verify_deployment", END)
graph.add_edge("handle_failure", END)

devops_graph = graph.compile(checkpointer=checkpointer)
```

---

## 4.2 Infrastructure Capability

### New Tools

#### list_allocations

```python
@tool
async def list_allocations(project_id: str | None = None) -> dict:
    """
    List allocated resources.

    Args:
        project_id: Filter by project (optional, shows all if not specified)

    Returns:
        {
            "allocations": [
                {
                    "id": "alloc_123",
                    "project_id": "hello-world-bot",
                    "server_handle": "vps-267179",
                    "server_ip": "1.2.3.4",
                    "port": 8080,
                    "allocated_at": "2024-01-15T10:00:00Z"
                },
                ...
            ]
        }
    """
    if project_id:
        allocations = await api_client.get_project_allocations(project_id)
    else:
        allocations = await api_client.get_all_allocations(
            user_id=_context.state["user_id"]
        )

    return {"allocations": allocations}
```

#### release_port

```python
@tool
async def release_port(allocation_id: str, confirm: bool = False) -> dict:
    """
    Release allocated port and free resources.

    WARNING: This will make the deployed service inaccessible!

    Args:
        allocation_id: Allocation ID from list_allocations
        confirm: Must be True to proceed

    Returns:
        {"released": True} or {"error": "..."}
    """
    if not confirm:
        return {
            "error": "Set confirm=True to release. This will stop the service!",
            "allocation_id": allocation_id,
        }

    allocation = await api_client.get_allocation(allocation_id)
    if not allocation:
        return {"error": f"Allocation {allocation_id} not found"}

    # Check ownership
    if allocation["user_id"] != _context.state["user_id"]:
        return {"error": "You can only release your own allocations"}

    await api_client.release_allocation(allocation_id)

    logger.info(
        "port_released",
        allocation_id=allocation_id,
        project_id=allocation["project_id"],
    )

    return {
        "released": True,
        "allocation_id": allocation_id,
        "project_id": allocation["project_id"],
    }
```

---

## 4.3 Engineering Capability

Same pattern as Deploy: Redis queue + subgraph.

### Tools

#### trigger_engineering

```python
@tool
async def trigger_engineering(
    project_id: str,
    task_description: str,
) -> dict:
    """
    Trigger code implementation pipeline (Analyst → Developer → Tester).

    Args:
        project_id: Project to work on
        task_description: What to implement

    Returns:
        {"job_id": "eng_xxx", "status": "queued"}
    """
    job_id = f"eng_{project_id}_{uuid4().hex[:8]}"

    await redis.xadd(
        ENGINEERING_QUEUE,
        {
            "job_id": job_id,
            "project_id": project_id,
            "task_description": task_description,
            "user_id": str(_context.state["telegram_user_id"]),
        },
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "message": f"Engineering task queued. Use get_engineering_status('{job_id}') to check.",
    }
```

#### get_engineering_status

```python
@tool
async def get_engineering_status(job_id: str) -> dict:
    """
    Check engineering pipeline progress.

    Returns:
        {
            "status": "queued|analyzing|implementing|testing|success|failed",
            "current_stage": "Developer",
            "iterations": 2,
            "pr_url": "https://github.com/...",  # if success
        }
    """
    config = {"configurable": {"thread_id": job_id}}
    checkpoint = await checkpointer.aget(config)

    if not checkpoint:
        return {"status": "not_found"}

    state = checkpoint.get("channel_values", {})

    return {
        "status": state.get("engineering_status"),
        "current_stage": state.get("current_agent"),
        "iterations": state.get("engineering_iterations", 0),
        "pr_url": state.get("pr_url"),
        "error": state.get("engineering_error"),
    }
```

#### view_latest_pr

```python
@tool
async def view_latest_pr(project_id: str) -> dict:
    """
    Get latest PR created for project.

    Returns:
        {
            "pr_url": "https://github.com/...",
            "title": "Add feature X",
            "status": "open|merged|closed",
            "created_at": "...",
        }
    """
    prs = await github_client.list_prs(project_id, limit=1)

    if not prs:
        return {"error": "No PRs found for this project"}

    pr = prs[0]
    return {
        "pr_url": pr["url"],
        "title": pr["title"],
        "status": pr["state"],
        "created_at": pr["created_at"],
        "author": pr["author"],
    }
```

---

## 4.4 Diagnose Capability

### Tools

#### get_service_logs

```python
@tool
async def get_service_logs(
    project_id: str,
    lines: int = 100,
    since: str | None = None,
) -> dict:
    """
    Fetch logs from running service.

    Args:
        project_id: Project identifier
        lines: Number of lines (default 100, max 500)
        since: ISO timestamp to fetch logs from (optional)

    Returns:
        {"logs": "...", "source": "docker|systemd"}
    """
    lines = min(lines, 500)

    # Get allocation to find server
    allocations = await api_client.get_project_allocations(project_id)
    if not allocations:
        return {"error": "Project has no deployed resources"}

    allocation = allocations[0]
    server = await api_client.get_server(allocation["server_id"])

    # Fetch logs via SSH/API
    logs = await infra_client.get_container_logs(
        server_handle=server["handle"],
        container_name=project_id,
        lines=lines,
        since=since,
    )

    return {
        "logs": logs,
        "source": "docker",
        "server": server["handle"],
        "lines_returned": len(logs.split("\n")),
    }
```

#### check_service_health

```python
@tool
async def check_service_health(project_id: str) -> dict:
    """
    Run health checks on deployed service.

    Returns:
        {
            "healthy": True/False,
            "checks": {
                "http": {"status": "ok", "response_time_ms": 45},
                "container": {"status": "running", "uptime": "2h 15m"},
                "memory": {"status": "ok", "usage_mb": 128},
            }
        }
    """
    allocations = await api_client.get_project_allocations(project_id)
    if not allocations:
        return {"error": "Project not deployed"}

    allocation = allocations[0]
    url = f"http://{allocation['server_ip']}:{allocation['port']}"

    checks = {}
    healthy = True

    # HTTP check
    try:
        start = time.time()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{url}/health", timeout=5)
        checks["http"] = {
            "status": "ok" if resp.status_code == 200 else "degraded",
            "response_time_ms": int((time.time() - start) * 1000),
            "status_code": resp.status_code,
        }
        if resp.status_code != 200:
            healthy = False
    except Exception as e:
        checks["http"] = {"status": "failed", "error": str(e)}
        healthy = False

    # Container check
    container_status = await infra_client.get_container_status(
        allocation["server_handle"],
        project_id,
    )
    checks["container"] = container_status
    if container_status.get("status") != "running":
        healthy = False

    return {
        "healthy": healthy,
        "checks": checks,
        "url": url,
    }
```

#### get_error_history

```python
@tool
async def get_error_history(
    project_id: str,
    hours: int = 24,
) -> dict:
    """
    Get recent errors from logs and monitoring.

    Args:
        project_id: Project identifier
        hours: How far back to look (default 24, max 168)

    Returns:
        {
            "errors": [
                {"timestamp": "...", "message": "...", "count": 5},
                ...
            ],
            "total_errors": 12
        }
    """
    hours = min(hours, 168)  # Max 1 week
    since = datetime.utcnow() - timedelta(hours=hours)

    # Query logs for errors
    logs = await get_service_logs.ainvoke({
        "project_id": project_id,
        "lines": 500,
        "since": since.isoformat(),
    })

    # Parse and aggregate errors
    error_lines = [
        line for line in logs.get("logs", "").split("\n")
        if "error" in line.lower() or "exception" in line.lower()
    ]

    # Group similar errors
    error_counts = {}
    for line in error_lines:
        # Simplified grouping — extract error type
        key = line[:100]  # First 100 chars as key
        error_counts[key] = error_counts.get(key, 0) + 1

    errors = [
        {"message": msg, "count": count}
        for msg, count in sorted(error_counts.items(), key=lambda x: -x[1])
    ][:20]  # Top 20 errors

    return {
        "errors": errors,
        "total_errors": len(error_lines),
        "time_range_hours": hours,
    }
```

---

## 4.5 Admin Capability

### Tools

#### list_graph_nodes

```python
@tool
def list_graph_nodes() -> dict:
    """
    List all available graph nodes that can be triggered manually.

    Returns:
        {
            "nodes": [
                {"name": "intent_parser", "type": "llm", "description": "..."},
                {"name": "product_owner", "type": "llm", "description": "..."},
                ...
            ]
        }
    """
    # Get from graph definition
    from ..graph import app

    nodes = []
    for name in app.nodes:
        nodes.append({
            "name": name,
            "type": "llm" if name in LLM_NODES else "function",
        })

    return {"nodes": nodes}
```

#### trigger_node_manually

```python
@tool
async def trigger_node_manually(
    node_name: str,
    project_id: str,
    extra_input: dict | None = None,
) -> dict:
    """
    Manually trigger a specific graph node.

    WARNING: Admin tool. Use with caution.

    Args:
        node_name: Node to trigger (from list_graph_nodes)
        project_id: Project context
        extra_input: Additional input for the node

    Returns:
        {"job_id": "manual_xxx", "status": "triggered"}
    """
    # Validate node exists
    if node_name not in app.nodes:
        return {"error": f"Unknown node: {node_name}"}

    job_id = f"manual_{node_name}_{uuid4().hex[:8]}"

    # Build minimal state for the node
    state = {
        "project_id": project_id,
        "user_id": _context.state["user_id"],
        "telegram_user_id": _context.state["telegram_user_id"],
        **(extra_input or {}),
    }

    # Queue for processing
    await redis.xadd(
        "admin:manual_triggers",
        {
            "job_id": job_id,
            "node_name": node_name,
            "state": json.dumps(state),
        },
    )

    return {
        "job_id": job_id,
        "status": "triggered",
        "node": node_name,
    }
```

#### clear_project_state

```python
@tool
async def clear_project_state(
    project_id: str,
    confirm: bool = False,
) -> dict:
    """
    Reset project state in orchestrator.

    WARNING: This clears all in-progress tasks and checkpoints for the project.

    Args:
        project_id: Project to reset
        confirm: Must be True to proceed

    Returns:
        {"cleared": True, "checkpoints_deleted": N}
    """
    if not confirm:
        return {"error": "Set confirm=True to clear state. This is destructive!"}

    # Find all checkpoints for this project
    # Pattern: thread_id contains project_id
    deleted = 0

    # Clear deploy jobs
    deploy_threads = await redis.keys(f"deploy_{project_id}_*")
    for thread in deploy_threads:
        await checkpointer.adelete({"configurable": {"thread_id": thread}})
        deleted += 1

    # Clear engineering jobs
    eng_threads = await redis.keys(f"eng_{project_id}_*")
    for thread in eng_threads:
        await checkpointer.adelete({"configurable": {"thread_id": thread}})
        deleted += 1

    logger.warning(
        "project_state_cleared",
        project_id=project_id,
        checkpoints_deleted=deleted,
        cleared_by=_context.state["telegram_user_id"],
    )

    return {
        "cleared": True,
        "project_id": project_id,
        "checkpoints_deleted": deleted,
    }
```

---

## Worker Deployment

### Docker Compose Addition

```yaml
# docker-compose.yml
services:
  deploy-worker:
    build:
      context: .
      dockerfile: services/langgraph/Dockerfile
    command: python -m src.workers.deploy_worker
    environment:
      - REDIS_URL=${REDIS_URL}
      - DATABASE_URL=${DATABASE_URL}
      - LANGCHAIN_TRACING_V2=true
    depends_on:
      - redis
      - postgres
    restart: unless-stopped
    deploy:
      replicas: 2  # Scale as needed

  engineering-worker:
    build:
      context: .
      dockerfile: services/langgraph/Dockerfile
    command: python -m src.workers.engineering_worker
    environment:
      - REDIS_URL=${REDIS_URL}
      - DATABASE_URL=${DATABASE_URL}
    depends_on:
      - redis
      - postgres
    restart: unless-stopped
```

---

## Checklist

### 4.0 Infrastructure
- [x] Add `DEPLOY_QUEUE`, `ENGINEERING_QUEUE` constants to redis_client
- [x] Create `ensure_consumer_groups()` utility
- [x] Add checkpointer access utilities

### 4.1 Deploy Capability
- [x] Implement `trigger_deploy` tool
- [x] Implement `get_deploy_status` tool
- [x] Implement `get_deploy_logs` tool
- [x] Update `check_deploy_readiness` tool
- [x] Create `deploy_worker.py`
- [x] Create `devops_subgraph.py` (Using devops_node for now)
- [x] Add deploy-worker to docker-compose

### 4.2 Infrastructure Capability
- [x] Implement `list_allocations` tool
- [x] Implement `release_port` tool
- [x] Add API endpoints for allocations

### 4.3 Engineering Capability
- [x] Implement `trigger_engineering` tool
- [x] Implement `get_engineering_status` tool
- [x] Implement `view_latest_pr` tool
- [x] Create `engineering_worker.py`
- [ ] Refactor existing engineering subgraph (Worker uses placebo for now)

### 4.4 Diagnose Capability
- [x] Implement `get_service_logs` tool
- [x] Implement `check_service_health` tool
- [x] Implement `get_error_history` tool
- [x] Add infra_client methods for container logs

### 4.5 Admin Capability
- [x] Implement `list_graph_nodes` tool
- [x] Implement `trigger_node_manually` tool
- [x] Implement `clear_project_state` tool
- [x] Create admin_worker.py (Integrated into shared queue logic)

### 4.6 Integration
- [x] Register all tools in CAPABILITY_REGISTRY
- [x] Update TOOLS_MAP with new tools
- [ ] Add workers to Makefile
- [x] Update docker-compose.yml

### 4.7 Testing
- [x] Unit tests for each tool
- [x] Integration test: trigger_deploy → poll status → success (Manual smoke test)
- [x] Integration test: worker crash recovery (Manual verification)
- [ ] Load test: concurrent deployments

---

## Open Questions

| Question | Status | Decision |
|----------|--------|----------|
| cancel_deploy needed? | Deferred | Not in v1 |
| Job retention period | **Done** | 7 days (Redis TTL) |
| Max concurrent deploys per user | **Done** | 3 (Enforced in tool) |
| Notification on job complete | **TBD** | Push to Telegram? |

---

## Next Phase

After Phase 4, proceed to [Phase 5: Integration & Testing](./phase5-integration.md) (to be created).
