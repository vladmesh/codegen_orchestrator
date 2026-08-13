import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import json
import os
import subprocess
import time
from uuid import uuid4

from docker.errors import NotFound
import pytest
import redis.asyncio as redis

import docker
from scripts.shared_freshness import source_hash
from shared.contracts.queues.worker import CreateWorkerResponse
from shared.queues import WORKER_MANAGER_GROUP

# Configure pytest-asyncio
pytest_plugins = ("pytest_asyncio",)


def pytest_configure(config):
    """Configure pytest-asyncio mode."""
    config.addinivalue_line("markers", "integration: mark test as integration test")


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DOCKER_HOST = os.getenv("DOCKER_HOST", "tcp://docker:2375")
API_BASE_URL = os.getenv("API_BASE_URL", "http://172.31.0.20:8000")

# The repository as this container sees it: shared/, packages/ and services/ are mounted
# under /app, which is what source_hash() hashes.
TREE_ROOT = "/app"

# Stream constants
REDIS_STREAM_COMMANDS = "worker:commands"
REDIS_STREAM_DEV_RESPONSES = "worker:responses:developer"
TEST_TELEGRAM_ID = "999000"


# --- Shared test helpers ---


async def wait_for_create_response(
    redis_client: redis.Redis, stream: str, request_id: str, timeout: int = 120
) -> CreateWorkerResponse:
    """Wait for a CreateWorkerResponse matching the given request_id.

    Skips messages from other commands (e.g. delete responses from cleanup).
    """
    start = time.time()
    current_id = "0"
    while time.time() - start < timeout:
        messages = await redis_client.xread({stream: current_id}, count=1, block=1000)
        if not messages:
            continue
        msg_id = messages[0][1][0][0]
        fields = messages[0][1][0][1]
        current_id = msg_id

        data_str = fields.get("data") if isinstance(fields.get("data"), str) else None
        if not data_str:
            raw = fields.get(b"data")
            if raw:
                data_str = raw.decode() if isinstance(raw, bytes) else raw
        if not data_str:
            continue

        parsed = json.loads(data_str)
        if parsed.get("request_id") != request_id:
            continue

        response = CreateWorkerResponse.model_validate(parsed)
        if response.success and response.worker_id:
            await _wait_for_create_command_completion(
                redis_client,
                request_id=request_id,
                worker_id=response.worker_id,
                timeout=timeout,
            )
        return response

    raise TimeoutError(f"No response for request_id={request_id} on {stream} within {timeout}s")


async def _wait_for_create_command_completion(
    redis_client: redis.Redis, *, request_id: str, worker_id: str, timeout: int
) -> None:
    """Wait past the create command's early response until its stream entry is ACKed."""
    command_id = None
    for msg_id, fields in await redis_client.xrevrange(REDIS_STREAM_COMMANDS, count=100):
        data = fields.get("data")
        if data and json.loads(data).get("request_id") == request_id:
            command_id = msg_id
            break
    if command_id is None:
        raise RuntimeError(f"Create command not found for request_id={request_id}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = await redis_client.xpending_range(
            REDIS_STREAM_COMMANDS,
            WORKER_MANAGER_GROUP,
            min=command_id,
            max=command_id,
            count=1,
        )
        if not pending:
            status = await redis_client.hget(f"worker:status:{worker_id}", "status")
            if status == "RUNNING":
                return
            error = await redis_client.get(f"worker:error:{worker_id}")
            raise RuntimeError(
                f"Worker {worker_id} finished creation with status={status!r}: "
                f"{error or 'unknown error'}"
            )
        await asyncio.sleep(0.25)

    raise TimeoutError(
        f"Worker manager did not finish create command for {worker_id} within {timeout}s"
    )


async def wait_for_stream_message(
    redis_client: redis.Redis, stream: str, timeout: int = 30, last_id: str = "0"
) -> dict:
    """Wait for a message on Redis stream."""
    start = time.time()
    current_id = last_id
    while time.time() - start < timeout:
        messages = await redis_client.xread({stream: current_id}, count=1, block=1000)
        if messages:
            msg_id = messages[0][1][0][0]
            fields = messages[0][1][0][1]
            result = {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in fields.items()
            }
            result["_msg_id"] = msg_id
            return result
    raise TimeoutError(f"No message received on {stream} within {timeout}s")


@pytest.fixture
async def redis_client():
    client = redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def docker_client():
    client = docker.DockerClient(base_url=DOCKER_HOST)
    original_get = client.containers.get
    resolved_names: set[str] = set()

    def get_after_create(container_id: str):
        """Wait on the first lookup because create responses are acceptance ACKs."""
        if container_id in resolved_names:
            return original_get(container_id)
        deadline = time.time() + 180
        while True:
            try:
                container = original_get(container_id)
                resolved_names.add(container_id)
                return container
            except NotFound:
                if time.time() >= deadline:
                    raise
                time.sleep(0.25)

    client.containers.get = get_after_create
    yield client
    client.close()


# --- API client + data seeding ---


@pytest.fixture
async def api_client():
    """Async HTTP client for the API service."""
    import httpx

    internal_api_key = os.environ["INTERNAL_API_KEY"]
    async with httpx.AsyncClient(
        base_url=API_BASE_URL,
        timeout=10,
        headers={"X-Internal-Key": internal_api_key},
    ) as client:
        yield client


@pytest.fixture
async def user_api_client():
    """Async HTTP client for tests that must exercise user-scoped authorization.

    It carries the internal key and names the user per request, which is how the
    PO agent and the bot actually reach the API. The key does not make the
    isolation assertions vacuous: `resolve_actor` judges a request that names a
    user as that user, and `test_cross_user_access_denied` — a stranger refused
    with 403 while the key is present — is what proves it. Omitting the key is no
    longer an option either: since every route requires a credential, a keyless
    caller is answered 401 before the ownership check is ever reached.
    """
    import httpx

    internal_api_key = os.environ["INTERNAL_API_KEY"]
    async with httpx.AsyncClient(
        base_url=API_BASE_URL,
        timeout=10,
        headers={"X-Internal-Key": internal_api_key},
    ) as client:
        yield client


async def poll_task_status(
    api_client, task_id: str, target_statuses: set[str], timeout: int = 60
) -> dict:
    """Poll GET /api/tasks/{task_id} until status is in target_statuses."""
    start = time.time()
    while time.time() - start < timeout:
        resp = await api_client.get(f"/api/tasks/{task_id}")
        if resp.status_code == 200:
            task = resp.json()
            if task["status"] in target_statuses:
                return task
        await asyncio.sleep(1)
    raise TimeoutError(f"Task {task_id} did not reach {target_statuses} within {timeout}s")


@pytest.fixture
async def seed_project(api_client):
    """Factory fixture to create projects via API. Cleans up after test."""
    created_ids = []

    # Ensure test user exists (owner_id is NOT NULL)
    resp = await api_client.get(f"/api/users/by-telegram/{TEST_TELEGRAM_ID}")
    if resp.status_code == 404:
        await api_client.post(
            "/api/users/",
            json={
                "telegram_id": int(TEST_TELEGRAM_ID),
                "username": "integration_test",
                "first_name": "Test",
                "is_admin": True,
            },
        )

    async def _create(
        name: str = "Test Project",
        status: str = "draft",
        config: dict | None = None,
        repository_url: str | None = None,
        initiating_run_id: str = "backend-integration-run",
    ) -> dict:
        body = {
            "title": name,
            "status": status,
            "config": config or {},
            # Every project is created for a run — the seeded ones name this
            # suite as theirs, so the workers they lead to are attributable.
            "initiating_run_id": initiating_run_id,
        }
        if repository_url:
            body["repository_url"] = repository_url
        resp = await api_client.post(
            "/api/projects/",
            json=body,
            headers={"X-Telegram-ID": TEST_TELEGRAM_ID},
        )
        assert resp.status_code == 201, f"Failed to seed project: {resp.text}"
        data = resp.json()
        created_ids.append(data["id"])
        return data

    yield _create

    # Cleanup: DELETE cascades to tasks + allocations
    for pid in created_ids:
        with contextlib.suppress(Exception):
            await api_client.delete(f"/api/projects/{pid}")


@pytest.fixture
async def seed_task(api_client):
    """Factory fixture to create tasks via API."""

    async def _create(
        title: str = "Test Task",
        task_type: str = "feature",
        project_id: str | None = None,
        status: str = "backlog",
    ) -> dict:
        body = {"title": title, "type": task_type, "status": status}
        if project_id:
            body["project_id"] = project_id
        resp = await api_client.post("/api/tasks/", json=body)
        assert resp.status_code == 201, f"Failed to seed task: {resp.text}"
        return resp.json()

    yield _create


@pytest.fixture
async def seed_server(api_client):
    """Factory fixture to create servers via API."""

    async def _create(
        handle: str,
        host: str = "test.example.com",
        public_ip: str = "192.0.2.1",
        status: str = "ready",
        capacity_ram_mb: int = 8192,
        capacity_disk_mb: int = 51200,
        is_managed: bool = True,
    ) -> dict:
        body = {
            "handle": handle,
            "host": host,
            "public_ip": public_ip,
            "status": status,
            "capacity_ram_mb": capacity_ram_mb,
            "capacity_disk_mb": capacity_disk_mb,
            "is_managed": is_managed,
        }
        resp = await api_client.post("/api/servers/", json=body)
        assert resp.status_code == 201, f"Failed to seed server: {resp.text}"
        return resp.json()

    yield _create


@pytest.fixture(autouse=True)
def cleanup_worker_containers():
    """Remove any leftover worker containers before and after each test."""
    if os.getenv("BUILD_WORKER_BASE_IMAGES") != "true":
        yield
        return

    client = docker.DockerClient(base_url=DOCKER_HOST)

    def remove_workers():
        with contextlib.suppress(Exception):
            containers = client.containers.list(all=True)
            for container in containers:
                if container.name.startswith("worker-"):
                    with contextlib.suppress(Exception):
                        container.remove(force=True)

    # Cleanup before test
    remove_workers()

    yield

    # Cleanup after test
    remove_workers()
    client.close()


@pytest.fixture(autouse=True)
async def cleanup_redis_streams(redis_client):
    """Clean up Redis response streams BEFORE and after each test.

    Note: We do NOT delete worker:commands because worker-manager uses consumer groups.
    Deleting the stream would break the consumer group and worker-manager would stop working.
    """
    # Only clean response/output streams, NOT worker:commands (has consumer group)
    streams_to_clean = [
        "worker:responses:developer",
        "worker:developer:input",
        "worker:developer:output",
    ]

    async def cleanup():
        for stream in streams_to_clean:
            with contextlib.suppress(Exception):
                await redis_client.delete(stream)

    # Cleanup BEFORE test (important to avoid reading stale messages)
    await cleanup()

    yield

    # Cleanup after test
    await cleanup()


WORKSPACE_BASE_PATH = "/tmp/codegen/workspaces"  # noqa: S108


def _create_scaffolded_workspace() -> str:
    """Create a minimal git repo at /tmp/codegen/workspaces/{repo_id}/. Returns repo_id."""
    repo_id = str(uuid4())
    ws_path = os.path.join(WORKSPACE_BASE_PATH, repo_id)
    os.makedirs(ws_path, exist_ok=True)

    # Initialize a minimal git repo (workers expect a git workspace)
    subprocess.run(["git", "init", ws_path], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", ws_path, "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", ws_path, "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    # Create an initial commit so HEAD exists
    readme = os.path.join(ws_path, "README.md")
    with open(readme, "w") as f:
        f.write("# test\n")
    subprocess.run(["git", "-C", ws_path, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", ws_path, "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return repo_id


@pytest.fixture
def scaffolded_workspace():
    """Create a temporary pre-scaffolded workspace with a minimal git repo.

    Returns the repo_id (UUID string). The workspace is created at
    /tmp/codegen/workspaces/{repo_id}/ which is shared with worker-manager
    and DinD via the 'workspaces' named volume.
    """
    import shutil

    repo_id = _create_scaffolded_workspace()
    yield repo_id
    shutil.rmtree(os.path.join(WORKSPACE_BASE_PATH, repo_id), ignore_errors=True)


_SKIP_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    "node_modules",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
}


def _content_hash(*paths: str) -> str:
    """SHA256 hash of file/directory contents for cache invalidation.

    Not a producer of `SOURCE_HASH` — that value comes from `source_hash()` in
    `scripts/shared_freshness.py`, the only place it is computed. What is left here is
    the cache key of a derived worker image (`_child_image_hash` below).
    """
    h = hashlib.sha256()
    for path in sorted(paths):
        if os.path.isfile(path):
            with open(path, "rb") as f:
                h.update(f.read())
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    h.update(fp.encode())
                    with open(fp, "rb") as fh:
                        h.update(fh.read())
    return h.hexdigest()[:12]


def _child_image_hash(dockerfile: str, common_hash: str) -> str:
    """Cache key of a worker image derived from worker-base-common.

    The common image's hash goes in as bytes of its own. Handing it to
    _content_hash as if it were a path hashed nothing at all: that function reads
    files and directories, so a rebuilt common left every child tag unchanged and
    the cache-hit branch below kept retagging the stale child as :latest.
    """
    h = hashlib.sha256()
    h.update(_content_hash(dockerfile).encode())
    h.update(common_hash.encode())
    return h.hexdigest()[:12]


def _build_base_image(
    client,
    dockerfile_path: str,
    tag: str,
    shared_path: str,
    packages_path: str,
    source_hash: str,
    base_image: str | None = None,
):
    """Build a worker base image, skipping if a cached version exists.

    base_image is the BASE_IMAGE build arg the derived Dockerfiles require; common
    has no base of its own and passes None.
    """
    import shutil
    import tempfile

    # Check if image with this content hash already exists in DinD
    try:
        client.images.get(tag)
        print(f"  {tag} found in cache, skipping build")
        return
    except docker.errors.ImageNotFound:
        pass

    with tempfile.TemporaryDirectory() as tmp_dir:
        print(f"Building {tag}...")

        dest_dockerfile = os.path.join(tmp_dir, "Dockerfile")
        shutil.copy(dockerfile_path, dest_dockerfile)

        # Copy shared and packages (ignore dev artifacts with dangling symlinks)
        _ignore = shutil.ignore_patterns(*_SKIP_DIRS)
        shutil.copytree(shared_path, os.path.join(tmp_dir, "shared"), ignore=_ignore)
        shutil.copytree(packages_path, os.path.join(tmp_dir, "packages"), ignore=_ignore)

        buildargs = {"SOURCE_HASH": source_hash}
        if base_image is not None:
            buildargs["BASE_IMAGE"] = base_image

        try:
            image, build_logs = client.images.build(
                path=tmp_dir,
                tag=tag,
                rm=True,
                nocache=False,  # Allow cache for faster rebuilds
                buildargs=buildargs,
            )
            for chunk in build_logs:
                if "stream" in chunk:
                    print(chunk["stream"], end="")

            print(f"{tag} built successfully.")

            # Verify worker user exists
            output = client.containers.run(tag, "id worker", remove=True, entrypoint="/bin/sh -c")
            print(f"  Verified: {output.decode().strip()}")

        except docker.errors.BuildError as e:
            print(f"Build failed for {tag}!")
            for chunk in e.build_log:
                if "stream" in chunk:
                    print(chunk["stream"], end="")
            pytest.exit(f"Failed to build {tag}: {e}")
        except Exception as e:
            print(f"Failed to build {tag}: {e}")
            pytest.exit(f"Failed to build {tag}: {e}")


@pytest.fixture(scope="session", autouse=True)
def setup_worker_base_images():
    """Build agent-specific worker base images in DIND.

    Uses content hashing to skip rebuilds when source files haven't changed.
    DinD volume persists between runs, so cached images survive restarts.

    Build order: common (sequential) -> claude + factory (parallel).
    """
    if os.getenv("BUILD_WORKER_BASE_IMAGES") != "true":
        return

    client = docker.DockerClient(base_url=DOCKER_HOST)

    # Source paths mapped in integration-test-runner container
    shared_path = f"{TREE_ROOT}/shared"
    packages_path = f"{TREE_ROOT}/packages"
    images_dir = f"{TREE_ROOT}/services/worker-manager/images"

    # Compute content hashes for cache invalidation
    common_dockerfile = f"{images_dir}/worker-base-common/Dockerfile"
    claude_dockerfile = f"{images_dir}/worker-base-claude/Dockerfile"
    factory_dockerfile = f"{images_dir}/worker-base-factory/Dockerfile"

    # The hash of the tree, from the only place it is computed: the same function the
    # Makefile and the freshness check read, so what this fixture stamps on the image is
    # comparable with what the tree says.
    common_hash = source_hash(TREE_ROOT)
    # Child images depend on common hash + their own Dockerfile
    claude_hash = _child_image_hash(claude_dockerfile, common_hash)
    factory_hash = _child_image_hash(factory_dockerfile, common_hash)

    common_tag = f"worker-base-common:{common_hash}"
    claude_tag = f"worker-base-claude:{claude_hash}"
    factory_tag = f"worker-base-factory:{factory_hash}"

    try:
        # Build common first (claude and factory depend on it).
        # All three carry the same SOURCE_HASH label: worker-manager reads it off the base
        # image to build the runtime worker tag, and a mismatch between common and its
        # derivatives would split that tag within one run.
        _build_base_image(
            client, common_dockerfile, common_tag, shared_path, packages_path, common_hash
        )
        # Build claude + factory in parallel (independent of each other).
        # Both are layered on the content-hash tag built above, not on a :latest alias.
        with ThreadPoolExecutor(max_workers=2) as executor:
            f_claude = executor.submit(
                _build_base_image,
                client,
                claude_dockerfile,
                claude_tag,
                shared_path,
                packages_path,
                common_hash,
                common_tag,
            )
            f_factory = executor.submit(
                _build_base_image,
                client,
                factory_dockerfile,
                factory_tag,
                shared_path,
                packages_path,
                common_hash,
                common_tag,
            )
            f_claude.result()
            f_factory.result()

        # Tag as :latest for worker-manager image builder
        client.images.get(claude_tag).tag("worker-base-claude", "latest")
        client.images.get(factory_tag).tag("worker-base-factory", "latest")

    finally:
        client.close()
