# Service: Worker Manager

**Service Name:** `worker-manager`
**Current Name:** `workers-spawner` (requires rename)
**Responsibility:** Lifecycle Management of ephemeral Worker containers.

## 1. Responsibilities

The `worker-manager` is responsible for:
1.  **Spawning** new Worker containers with specific configurations.
2.  **Configuring** the Worker environment (injecting instructions, tools, modules).
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
    *   *Mechanism*: Generates a rigid system prompt appended to instructions.
    *   *Post-MVP*: Enforced by a proxy or wrapper shim.
*   `modules` (List[Enum]):
    *   `GIT`
    *   `GITHUB_CLI`
    *   `COPIER`
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
*   `status`: `STARTING` | `RUNNING` | `STOPPED` | `FAILED`
*   `ip_address`: Internal IP (if needed)

## 3. Worker Architecture (The "Wrapper")

Inside the container, there is a **Python Wrapper** (part of `worker-base`) that acts as the agent's nervous system.

### 3.1 Communication Flow (Data Plane)

1.  **Input Queue** (`worker:{id}:input`):
    *   The Wrapper listens to this Redis Stream.
    *   Ideally, the User/PO pushes commands here directly.
    *   For "Headless" mode: The `worker-manager` *can* push the initial prompt here after spawning, but subsequent interaction happens directly.

2.  **Output Queue** (`worker:{id}:output`):
    *   The Wrapper captures `stdout`, `stderr`, and any explicit tool outputs.
    *   Pushes structured events to Redis:
        ```json
        {
          "type": "stdout",
          "content": "I am checking the files...",
          "timestamp": "..."
        }
        ```

3.  **Headless Execution**:
    *   If a Worker is spawned for a single task, the Spawner can optionally inject the initial prompt into `worker:{id}:input` immediately after creating the container.
    *   The Worker runs, processes the input, and can signal completion via a special output event.

## 4. Open Decisions

### 4.1 Modules & Image Strategy (Local Cache + LRU GC)

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
