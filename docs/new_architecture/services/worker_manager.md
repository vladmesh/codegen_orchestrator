# Service: Worker Manager

**Service Name:** `worker-manager`
**Current Name:** `workers-spawner` (requires rename)
**Responsibility:** Lifecycle Management of ephemeral Worker containers.

## 1. Responsibilities

The `worker-manager` is responsible for:
1.  **Spawning** new Worker containers with specific configurations.
2.  **Configuring** the Worker environment (injecting instructions, tools, capabilities).
3.  **Terminating** Workers when they are no longer needed.
4.  **Monitoring** Worker status (health, existence).

It acts as the **Control Plane** for Workers. It does NOT handle the heavy lifting of message passing (Data Plane) - that is done directly between the Worker and the Orchestrator/User via Redis.

## 2. API (Redis Commands)

The manager listens to `worker:commands`.

### 2.1 Spawn Worker (`create`)

**Parameters:**

*   `agent_type` (Enum):
    *   `FACTORY` - Factory.ai Droid
    *   `CLAUDE` - Claude Code
    *   *(Future)* `CUSTOM`
*   `instructions` (String):
    *   Content for `CLAUDE.md` or `AGENTS.md`.
    *   Injected into the container at startup.
*   `allowed_commands` (List[String]):
    *   List of CLI commands the worker is permitted to execute.
    *   Format: `["project.get", "project.list", "engineering.*"]` (wildcards supported).
    *   *Mechanism*: Passed as `ALLOWED_COMMANDS` env var. CLI validates before execution.
    *   See [cli_orchestrator.md](../tools/cli_orchestrator.md#4-permission-model) for details.
*   `capabilities` (List[Enum]):
    *   `GIT`
    *   `GITHUB_CLI`
    *   `CURL`
    *   `DOCKER` (dind)
    *   *Mechanism*: Spawner decides how to install these (e.g., enable pre-installed features, mount binaries, or `apt-get` on boot).

**Returns:** `worker_id` (UUID)

### 2.2 Terminate Worker (`delete`)

**Parameters:**
*   `worker_id` (UUID)

### 2.3 Get Status (`status`)

**Parameters:**
*   `worker_id` (UUID)

**Returns:**
*   `status`: `starting` | `running` | `stopped` | `failed`
*   `ip_address`: Internal IP (if needed)

## 3. Worker Architecture (The "Wrapper")

Inside the container, there is a **Python Wrapper** (part of `worker-base`) that acts as the agent's nervous system.

### 3.1 Communication Flow (Data Plane)

Queue naming depends on worker type:

| Worker Type | Input Queue | Output Queue | Lifecycle |
|-------------|-------------|--------------|-----------|
| **PO** | `worker:po:{user_id}:input` | `worker:po:{user_id}:output` | Long-lived, per-user |
| **Developer** | `worker:developer:input` | `worker:developer:output` | Ephemeral, per-task |

1.  **Input Queue**:
    *   The Wrapper listens to this Redis Stream.
    *   PO: Telegram Bot pushes user messages.
    *   Developer: LangGraph pushes task specification.

2.  **Output Queue**:
    *   The Wrapper captures agent output and pushes structured events:
        ```json
        {
          "request_id": "...",
          "status": "success",
          "response": "Done. Created PR #42.",
          "duration_ms": 45000
        }
        ```

3.  **Headless Execution** (Developer Workers):
    *   LangGraph spawns worker and pushes task to `worker:developer:input`.
    *   Worker processes, publishes result, exits.
    *   Stateless — context is code in repo + error messages.

## 4. Open Decisions

### 4.1 Capabilities & Image Strategy (Local Cache + LRU GC)

*   **Concern**: Uncontrolled growth of Docker artifacts (layers/images) on disk.
*   **Solution**: **Local Registry with Active Garbage Collection**.
    *   **Storage**: Use the local Docker Daemon. It's the most efficient because of Layer Deduplication (10 variants share 99% of bytes). External registries (GHCR/DockerHub) introduce unnecessary network latency.
    *   **Metadata**: Store `last_used_at` timestamp for each `image_hash` in Redis.
    *   **Garbage Collection**:
        *   Background task in `worker-manager` runs daily.
        *   Checks `last_used_at`.
        *   Executes `docker rmi worker:<hash>` for images unused for > 7 days (configurable).
        *   Executes `docker builder prune` to clean build cache.

*   **Workflow**:
    1.  **Hash**: `h = sha256(...)`
    2.  **Check**: `docker image inspect worker:<h>`
    3.  **Build**: If missing -> Generate Dockerfile -> `docker build -t worker:<h> .`
    4.  **Touch**: Update `last_used_at` in Redis.
    5.  **Run**: `docker run ...`

*   **Why strict Dockerfile?**: We avoid `docker commit`. Everything must be reproducible from a Dockerfile for debugging.

### 4.2 Spawner vs Worker Responsibility
*   **Spawner**: Creates the "Body" (Container + Env Vars + Config Files).
*   **Worker**: Has a "Brain" (Wrapper) that listens to the "Nervous System" (Redis).
*   **Verdict**: Spawner does NOT proxy all messages. It sets up the channel, then steps back. This prevents Spawner from becoming a bottleneck and SPOF for active sessions.

## 5. Worker Status Monitoring

Worker-manager maintains worker status for sync lookup by other services (Telegram Bot).

### 5.1 Status Storage

Redis hash per worker:
```
worker:status:{id} = {
    "status": "STARTING|RUNNING|COMPLETED|FAILED|STOPPED",
    "started_at": "2026-01-10T12:00:00Z",
    "updated_at": "2026-01-10T12:05:00Z",
    "exit_code": 0,
    "error": null
}
```

### 5.2 Detection Mechanisms (Hybrid)

1.  **Docker Events API** (passive, reliable):
    *   Worker-manager subscribes to Docker daemon events.
    *   Catches: `start`, `die`, `oom`, `stop`.
    *   Guarantees detection even on SIGKILL.

2.  **Explicit Exit Events** (active, rich context):
    *   Wrapper inside worker publishes to `worker:lifecycle` stream before exit:
        ```json
        {"worker_id": "...", "event": "completed", "result": {...}}
        {"worker_id": "...", "event": "failed", "error": "..."}
        ```
    *   Provides task result and error details.

3.  **Heartbeat** (optional, for long-running workers):
    *   Wrapper sets `SETEX worker:heartbeat:{id} 30 "alive"`.
    *   Missing key = worker is dead.

### 5.3 Consumer Usage

Telegram Bot checks status synchronously:
```python
status = redis.hgetall(f"worker:status:{worker_id}")
if status["status"] in ("STOPPED", "FAILED"):
    # Create new worker
```

---

## 6. Resource Management (Pause/Resume)

To prevent resource exhaustion, the Manager implements an **Idle Pause** mechanism.

### 6.1 Pause Logic (`pause_idle_workers`)
A background task runs periodically (e.g., every 5 minutes):
1.  Scans all `RUNNING` workers.
2.  Checks `last_activity` in `worker:status:{id}`.
3.  If `now() - last_activity > IDLE_TIMEOUT_SECONDS`:
    *   Executes `docker pause {container_id}`.
    *   Updates Redis status to `PAUSED`.
    *   **Constraint**: `IDLE_TIMEOUT` MUST be > `TASK_EXECUTION_TIMEOUT` to avoid pausing active tasks.

### 6.2 Wakeup Logic (`wakeup_workers`)
A background task monitors Input Queues for `PAUSED` workers:
1.  Scans input streams for all `PAUSED` workers.
2.  If `xlen(stream) > 0`:
    *   Executes `docker unpause {container_id}`.
    *   Updates Redis status to `RUNNING`.
3.  Wrapper inside the container resumes `XREAD` immediately (as process was frozen).

---

## 7. Worker Container Specification

### 6.1 Base Image (`worker-base`)

The base image is **minimal** — it contains only what's needed for orchestration:

```dockerfile
# docker/Dockerfile.worker-base
FROM python:3.12-slim

# System deps for orchestrator-cli
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install orchestrator-cli (the ONLY pre-installed tool)
COPY packages/orchestrator-cli /app/orchestrator-cli
RUN pip install --no-cache-dir /app/orchestrator-cli

# Wrapper that listens to Redis and manages agent lifecycle
COPY packages/worker-wrapper /app/worker-wrapper
RUN pip install --no-cache-dir /app/worker-wrapper

# Entry point
ENTRYPOINT ["worker-wrapper"]
```

**What's included:**
- `python:3.12-slim` base
- `orchestrator-cli` — agent-to-system interface
- `worker-wrapper` — Redis listener + agent lifecycle manager
- `curl` — for health checks

**What's NOT included (added via capabilities):**
- `git` — added by `GIT` capability
- `gh` (GitHub CLI) — added by `GITHUB_CLI` capability
- `docker` — added by `DOCKER` capability (dind mount)

### 6.2 Capabilities & Layer Caching

Capabilities are installed on top of base image. Docker layer caching ensures fast builds:

```
worker-base (200MB)
    └── + GIT capability → worker:abc123 (210MB, cached)
    └── + GIT + DOCKER   → worker:ghi789 (250MB, cached)
```

Each unique combination of capabilities produces a deterministic image hash:
```python
def compute_image_hash(capabilities: list[str]) -> str:
    sorted_caps = sorted(set(capabilities))
    return hashlib.sha256(",".join(sorted_caps).encode()).hexdigest()[:12]
```

### 6.3 Wrapper Architecture

The **worker-wrapper** is a Python process that:

1. **Startup**: Reads config from environment variables
2. **Listen**: Subscribes to `worker:{id}:input` Redis stream
3. **Process**: Forwards messages to CLI-Agent (Claude Code / Factory Droid)
4. **Capture**: Collects agent output (stdout/stderr)
5. **Publish**: Sends output to `worker:{id}:output` stream
6. **Exit**: Reports completion/failure to `worker:lifecycle`

```
┌─────────────────────────────────────────────────────────┐
│                   WORKER CONTAINER                      │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   ┌─────────────────────────────────────────────────┐   │
│   │              worker-wrapper (PID 1)             │   │
│   │                                                 │   │
│   │  - Reads WORKER_ID, API_URL, ALLOWED_COMMANDS   │   │
│   │  - Subscribes to worker:{id}:input              │   │
│   │  - Spawns CLI-Agent subprocess                  │   │
│   │  - Captures output → worker:{id}:output         │   │
│   └─────────────────────────────────────────────────┘   │
│                           │                             │
│                           ▼                             │
│   ┌─────────────────────────────────────────────────┐   │
│   │            CLI-Agent (subprocess)               │   │
│   │                                                 │   │
│   │  Claude Code / Factory Droid                    │   │
│   │  Uses: orchestrator-cli, git, etc.              │   │
│   └─────────────────────────────────────────────────┘   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 6.4 Environment Variables

| Variable | Source | Purpose |
|----------|--------|---------|
| `WORKER_ID` | worker-manager | Unique container identifier |
| `API_URL` | worker-manager | Base URL for orchestrator-cli |
| `REDIS_URL` | worker-manager | Redis connection string |
| `ALLOWED_COMMANDS` | worker-manager | Permission list for CLI |
| `USER_ID` | worker-manager | Telegram user ID (for context) |
| `ANTHROPIC_API_KEY` | secrets | Claude API key (if Claude agent) |
| `OPENAI_API_KEY` | secrets | OpenAI key (if Factory agent) |

### 6.5 Lifecycle States

```
STARTING → RUNNING → COMPLETED
                  ↘ FAILED
                  ↘ STOPPED (manual kill)
```

Wrapper reports state changes to `worker:lifecycle` stream:
```json
{"worker_id": "abc123", "event": "started", "timestamp": "..."}
{"worker_id": "abc123", "event": "completed", "result": {...}}
{"worker_id": "abc123", "event": "failed", "error": "..."}
```
