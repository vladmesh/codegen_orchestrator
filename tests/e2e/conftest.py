import os

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

import docker

# Configure pytest-asyncio
pytest_plugins = ("pytest_asyncio",)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DOCKER_HOST = os.getenv("DOCKER_HOST", "tcp://docker:2375")


@pytest_asyncio.fixture
async def redis_client():
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.close()


def _build_base_image(client, dockerfile_path: str, tag: str, shared_path: str, packages_path: str):
    """Build a worker base image with given Dockerfile."""
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
    """Build agent-specific worker base images in DIND.

    Builds two images:
    - worker-base-claude: with Node.js + Claude CLI pre-installed
    - worker-base-factory: with Factory CLI pre-installed

    This ensures fast worker image builds during tests (only capabilities added).
    """
    client = docker.DockerClient(base_url=DOCKER_HOST)

    # Source paths mapped in integration-test-runner container
    shared_path = "/app/shared"
    packages_path = "/app/packages"

    # Agent-specific Dockerfiles
    images_to_build = [
        (
            "/app/services/worker-manager/images/worker-base-common/Dockerfile",
            "worker-base-common:latest",
        ),
        (
            "/app/services/worker-manager/images/worker-base-claude/Dockerfile",
            "worker-base-claude:latest",
        ),
        (
            "/app/services/worker-manager/images/worker-base-factory/Dockerfile",
            "worker-base-factory:latest",
        ),
    ]

    try:
        for dockerfile_path, tag in images_to_build:
            _build_base_image(client, dockerfile_path, tag, shared_path, packages_path)
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
