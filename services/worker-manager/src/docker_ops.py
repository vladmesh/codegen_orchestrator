import docker
import asyncio
from typing import Any, Dict, List, Tuple
import structlog
from concurrent.futures import ThreadPoolExecutor

logger = structlog.get_logger()


class DockerClientWrapper:
    """
    Async wrapper around blocking docker-py client.
    Abstracts Docker operations to allow mocking and non-blocking execution.
    """

    def __init__(self, base_url: str | None = None):
        self._client = docker.from_env()
        self._executor = ThreadPoolExecutor(max_workers=5)

    async def _run(self, func, *args, **kwargs):
        """Run blocking function in thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: func(*args, **kwargs))

    async def run_container(self, image: str, **kwargs) -> Any:
        """Run a container."""
        return await self._run(self._client.containers.run, image, **kwargs)

    async def get_container(self, container_id: str) -> Any:
        """Get a container by ID."""
        return await self._run(self._client.containers.get, container_id)

    async def list_containers(self, filters: Dict[str, Any] | None = None, all: bool = False) -> List[Any]:
        """List containers."""
        return await self._run(self._client.containers.list, all=all, filters=filters)

    async def stop_container(self, container_id: str, timeout: int = 10) -> None:
        """Stop a container."""
        container = await self.get_container(container_id)
        await self._run(container.stop, timeout=timeout)

    async def remove_container(self, container_id: str, force: bool = False, v: bool = False) -> None:
        """Remove a container."""
        # Use simple try/except for get in case it's already gone
        try:
            container = await self.get_container(container_id)
            await self._run(container.remove, force=force, v=v)
        except docker.errors.NotFound:
            pass

    async def pause_container(self, container_id: str) -> None:
        """Pause a container."""
        container = await self.get_container(container_id)
        await self._run(container.pause)

    async def unpause_container(self, container_id: str) -> None:
        """Unpause a container."""
        container = await self.get_container(container_id)
        await self._run(container.unpause)

    async def inspect_container(self, container_id: str) -> Dict[str, Any]:
        """Inspect a container."""
        # container attrs are cached, need to reload to get fresh status
        container = await self.get_container(container_id)
        # attrs property is already populated, but might be stale?
        # get() calls reload() implicitly? No, container object has .attrs.
        # But get() fetches fresh object.
        return container.attrs

    async def image_exists(self, image: str) -> bool:
        """Check if an image exists locally."""
        try:
            await self._run(self._client.images.get, image)
            return True
        except docker.errors.ImageNotFound:
            return False

    async def pull_image(self, image: str) -> Any:
        """Pull an image."""
        try:
            return await self._run(self._client.images.pull, image)
        except Exception:
            # Re-raise or handle? For now re-raise
            raise

    async def list_images(self, name: str | None = None, all: bool = False) -> List[Any]:
        """List images."""
        return await self._run(self._client.images.list, name=name, all=all)

    async def remove_image(self, image: str, force: bool = False) -> None:
        """Remove an image."""
        try:
            await self._run(self._client.images.remove, image, force=force)
        except docker.errors.ImageNotFound:
            pass

    async def build_image(self, dockerfile_content: str, tag: str) -> Any:
        """
        Build a Docker image from Dockerfile content.

        Args:
            dockerfile_content: Dockerfile content as string
            tag: Tag for the built image (e.g., "worker:abc123")

        Returns:
            Built image object
        """
        import io

        # Docker SDK expects a file-like object or path
        # We use fileobj with a BytesIO containing the Dockerfile
        dockerfile_bytes = dockerfile_content.encode("utf-8")

        def _build():
            # Create a minimal build context with just the Dockerfile
            import tarfile

            # Build context as tar archive
            context = io.BytesIO()
            with tarfile.open(fileobj=context, mode="w") as tar:
                # Add Dockerfile to the archive
                dockerfile_info = tarfile.TarInfo(name="Dockerfile")
                dockerfile_info.size = len(dockerfile_bytes)
                tar.addfile(dockerfile_info, io.BytesIO(dockerfile_bytes))

            context.seek(0)

            # Build the image
            image, build_logs = self._client.images.build(
                fileobj=context,
                custom_context=True,
                tag=tag,
                rm=True,  # Remove intermediate containers
                forcerm=True,  # Always remove intermediate containers
            )
            return image

        logger.info("building_image", tag=tag)
        return await self._run(_build)

    async def exec_in_container(self, container_id: str, command: str, user: str = "worker") -> Tuple[int, bytes]:
        """
        Execute a command in a running container.

        Args:
            container_id: ID of the container
            command: Command run
            user: User to run command as (default: "worker")

        Returns:
            Tuple of (exit_code, output_bytes)
        """
        container = await self.get_container(container_id)
        # exec_run is blocking, run in executor
        # returns (exit_code, output)
        return await self._run(container.exec_run, cmd=command, user=user)
