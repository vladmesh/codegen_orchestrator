import contextlib
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
    await client.close()


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
        "worker:responses:po",
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


@pytest.fixture(scope="session", autouse=True)
def setup_worker_base():
    """Build worker-base image in dind with correct context."""
    import os
    import shutil
    import tempfile

    # We need a docker client for session scope
    client = docker.DockerClient(base_url=DOCKER_HOST)

    # Source paths mapped in integration-test-runner container
    dockerfile_path = "/app/services/worker-manager/images/worker-base/Dockerfile"
    shared_path = "/app/shared"
    packages_path = "/app/packages"

    # Create temp context
    with tempfile.TemporaryDirectory() as tmp_dir:
        print(f"Preparing build context in {tmp_dir}...")

        dest_dockerfile = os.path.join(tmp_dir, "Dockerfile")
        shutil.copy(dockerfile_path, dest_dockerfile)

        # Debug: Print Dockerfile content
        with open(dest_dockerfile) as f:
            print(f"DEBUG: Dockerfile content:\n{f.read()}")

        # Copy shared
        shutil.copytree(shared_path, os.path.join(tmp_dir, "shared"))

        # Copy packages
        shutil.copytree(packages_path, os.path.join(tmp_dir, "packages"))

        print("Building worker-base...")
        try:
            # Build using low-level API to stream logs
            # client.images.build returns (image, logs) iterator if we use it differently,
            # OR we can use low level api.
            # Easiest: use client.images.build but print logs if it fails.
            # Actually client.images.build returns image object.
            # logs are lost unless we catch api error or usage json stream.

            image, build_logs = client.images.build(
                path=tmp_dir,
                tag="worker-base:latest",
                rm=True,
                nocache=False,  # Allow cache
            )
            for chunk in build_logs:
                if "stream" in chunk:
                    print(chunk["stream"], end="")

            print("worker-base built successfully.")

            # Verify user worker exists
            try:
                print("Verifying worker user...")
                # Override entrypoint to properly execute command
                output = client.containers.run(
                    "worker-base:latest", "id worker", remove=True, entrypoint="/bin/sh -c"
                )
                print(f"Verification success: {output.decode().strip()}")
            except Exception as e:
                print(f"Verification failed! User 'worker' not found in image: {e}")
                pytest.exit(f"Verification failed for worker-base: {e}")

        except docker.errors.BuildError as e:
            print("Build failed!")
            for chunk in e.build_log:
                if "stream" in chunk:
                    print(chunk["stream"], end="")
            pytest.exit(f"Failed to build worker-base: {e}")
        except Exception as e:
            print(f"Failed to build worker-base: {e}")
            pytest.exit(f"Failed to build worker-base: {e}")
        finally:
            client.close()
