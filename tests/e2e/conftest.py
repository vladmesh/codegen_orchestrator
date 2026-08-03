import hashlib
import os

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

import docker

# Configure pytest-asyncio
pytest_plugins = ("pytest_asyncio",)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DOCKER_HOST = os.getenv("DOCKER_HOST", "tcp://docker:2375")


def pytest_addoption(parser):
    parser.addoption(
        "--run-e2e-real",
        action="store_true",
        default=False,
        help="run e2e real llm tests",
    )


@pytest_asyncio.fixture
async def redis_client():
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.close()


_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules", ".venv"}


def _content_hash(*paths: str) -> str:
    """SHA256 of the sources copied into the worker build context."""
    h = hashlib.sha256()
    for path in sorted(paths):
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            for name in sorted(files):
                if name.endswith(".pyc"):
                    continue
                fp = os.path.join(root, name)
                h.update(os.path.relpath(fp, path).encode())
                with open(fp, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()[:16]


def _build_base_image(
    client,
    dockerfile_path: str,
    tag: str,
    shared_path: str,
    packages_path: str,
    source_hash: str,
    base_image: str | None = None,
):
    """Build a worker base image with given Dockerfile.

    base_image is the BASE_IMAGE build arg the derived Dockerfiles require; common
    has no base of its own and passes None.
    """
    import os
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        print(f"Preparing build context for {tag} in {tmp_dir}...")

        dest_dockerfile = os.path.join(tmp_dir, "Dockerfile")
        shutil.copy(dockerfile_path, dest_dockerfile)

        # Copy shared and packages
        shutil.copytree(shared_path, os.path.join(tmp_dir, "shared"))
        shutil.copytree(packages_path, os.path.join(tmp_dir, "packages"))

        print(f"Building {tag}...")
        buildargs = {"SOURCE_HASH": source_hash}
        if base_image is not None:
            buildargs["BASE_IMAGE"] = base_image

        try:
            image, build_logs = client.images.build(
                path=tmp_dir,
                tag=tag,
                rm=True,
                nocache=True,  # Force rebuild to pick up wrapper.py changes
                pull=False,  # Don't pull base images - use local ones (e.g. worker-base-common)
                buildargs=buildargs,
            )
            for chunk in build_logs:
                if "stream" in chunk:
                    print(chunk["stream"], end="")

            print(f"{tag} built successfully.")

            # Verify worker user exists
            print(f"Verifying worker user in {tag}...")
            output = client.containers.run(tag, "id worker", remove=True, entrypoint="/bin/sh -c")
            print(f"Verification success: {output.decode().strip()}")

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
    """Build agent-specific worker base images in DIND and create network.

    Builds two images:
    - worker-base-claude: with Node.js + Claude CLI pre-installed
    - worker-base-factory: with Factory CLI pre-installed

    Also creates the e2e-test-network inside DIND for worker containers.
    """
    client = docker.DockerClient(base_url=DOCKER_HOST)

    # Create network inside DIND for worker containers
    # This is needed because worker-manager creates workers that attach to this network
    try:
        client.networks.create("e2e-test-network", driver="bridge")
        print("Created e2e-test-network inside DIND")
    except docker.errors.APIError as e:
        if "already exists" in str(e):
            print("Network e2e-test-network already exists in DIND")
        else:
            raise

    # Source paths mapped in integration-test-runner container
    shared_path = "/app/shared"
    packages_path = "/app/packages"

    # Agent-specific Dockerfiles. The common image is built first; the derived ones
    # name it through BASE_IMAGE, which their Dockerfiles declare without a default.
    common_tag = "worker-base-common:latest"
    images_to_build = [
        (
            "/app/services/worker-manager/images/worker-base-common/Dockerfile",
            common_tag,
            None,
        ),
        (
            "/app/services/worker-manager/images/worker-base-claude/Dockerfile",
            "worker-base-claude:latest",
            common_tag,
        ),
        (
            "/app/services/worker-manager/images/worker-base-factory/Dockerfile",
            "worker-base-factory:latest",
            common_tag,
        ),
    ]

    # One hash for all three images: worker-manager reads the label off the base image to
    # build the runtime worker tag, so common and its derivatives must agree within a run.
    source_hash = _content_hash(shared_path, packages_path)

    try:
        for dockerfile_path, tag, base_image in images_to_build:
            _build_base_image(
                client, dockerfile_path, tag, shared_path, packages_path, source_hash, base_image
            )
    finally:
        client.close()


@pytest.fixture
def redis(redis_client):
    """Alias for redis_client to match test signature."""
    return redis_client


@pytest.fixture
def docker_client():
    client = docker.DockerClient(base_url=DOCKER_HOST)
    yield client
    client.close()
