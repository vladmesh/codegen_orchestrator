from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import os

import pytest
import redis.asyncio as redis

import docker

# Configure pytest-asyncio
pytest_plugins = ("pytest_asyncio",)


def pytest_configure(config):
    """Configure pytest-asyncio mode."""
    config.addinivalue_line("markers", "integration: mark test as integration test")


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DOCKER_HOST = os.getenv("DOCKER_HOST", "tcp://docker:2375")


@pytest.fixture
async def redis_client():
    client = redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def docker_client():
    client = docker.DockerClient(base_url=DOCKER_HOST)
    yield client
    client.close()


@pytest.fixture(autouse=True)
def cleanup_worker_containers(docker_client):
    """Remove any leftover worker containers before and after each test."""

    def remove_workers():
        with contextlib.suppress(Exception):
            containers = docker_client.containers.list(all=True)
            for container in containers:
                if container.name.startswith("worker-"):
                    with contextlib.suppress(Exception):
                        container.remove(force=True)

    # Cleanup before test
    remove_workers()

    yield

    # Cleanup after test
    remove_workers()


@pytest.fixture(autouse=True)
async def cleanup_redis_streams(redis_client):
    """Clean up Redis response streams BEFORE and after each test.

    Note: We do NOT delete worker:commands because worker-manager uses consumer groups.
    Deleting the stream would break the consumer group and worker-manager would stop working.
    """
    # Only clean response/output streams, NOT worker:commands (has consumer group)
    streams_to_clean = [
        "worker:responses:developer",
        "worker:lifecycle",
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


_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules"}


def _content_hash(*paths: str) -> str:
    """SHA256 hash of file/directory contents for cache invalidation."""
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


def _build_base_image(
    client,
    dockerfile_path: str,
    tag: str,
    shared_path: str,
    packages_path: str,
):
    """Build a worker base image, skipping if a cached version exists."""
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

        # Copy shared and packages
        shutil.copytree(shared_path, os.path.join(tmp_dir, "shared"))
        shutil.copytree(packages_path, os.path.join(tmp_dir, "packages"))

        try:
            image, build_logs = client.images.build(
                path=tmp_dir,
                tag=tag,
                rm=True,
                nocache=False,  # Allow cache for faster rebuilds
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
    client = docker.DockerClient(base_url=DOCKER_HOST)

    # Source paths mapped in integration-test-runner container
    shared_path = "/app/shared"
    packages_path = "/app/packages"
    images_dir = "/app/services/worker-manager/images"

    # Compute content hashes for cache invalidation
    common_dockerfile = f"{images_dir}/worker-base-common/Dockerfile"
    claude_dockerfile = f"{images_dir}/worker-base-claude/Dockerfile"
    factory_dockerfile = f"{images_dir}/worker-base-factory/Dockerfile"

    common_hash = _content_hash(common_dockerfile, shared_path, packages_path)
    # Child images depend on common hash + their own Dockerfile
    claude_hash = _content_hash(claude_dockerfile, common_hash)
    factory_hash = _content_hash(factory_dockerfile, common_hash)

    common_tag = f"worker-base-common:{common_hash}"
    claude_tag = f"worker-base-claude:{claude_hash}"
    factory_tag = f"worker-base-factory:{factory_hash}"

    try:
        # Build common first (claude and factory depend on it)
        _build_base_image(client, common_dockerfile, common_tag, shared_path, packages_path)
        # Also tag as :latest so child Dockerfiles (FROM worker-base-common:latest) work
        client.images.get(common_tag).tag("worker-base-common", "latest")

        # Build claude + factory in parallel (independent of each other)
        with ThreadPoolExecutor(max_workers=2) as executor:
            f_claude = executor.submit(
                _build_base_image,
                client,
                claude_dockerfile,
                claude_tag,
                shared_path,
                packages_path,
            )
            f_factory = executor.submit(
                _build_base_image,
                client,
                factory_dockerfile,
                factory_tag,
                shared_path,
                packages_path,
            )
            f_claude.result()
            f_factory.result()

        # Tag as :latest for worker-manager image builder
        client.images.get(claude_tag).tag("worker-base-claude", "latest")
        client.images.get(factory_tag).tag("worker-base-factory", "latest")

    finally:
        client.close()
